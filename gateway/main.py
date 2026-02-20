import asyncio
import base64
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, Response
from docker.errors import NotFound
from starlette.responses import StreamingResponse

from gateway.models import (
    BinaryFileContent, CreateSandboxRequest, ExecRequest, FileContent,
    HealthResponse, SandboxInfo, DirListing, DirEntry,
    BrowserNavigateRequest, BrowserScreenshotRequest,
    BrowserClickRequest, BrowserTypeRequest,
)
from gateway import sandbox, executor, files, browser
from gateway.sandbox import SandboxStateError
from gateway.auth import verify_api_key
from gateway.events import publish as emit_event, sse_stream
from gateway.gc import gc_loop, collect_garbage
from gateway.logging_config import setup_logging, request_id_var, sandbox_id_var
from gateway.metrics import metrics_response_bytes, requests_total
from gateway.recovery import recover_state
from gateway.pool import pool_loop, warm_pool_count, drain_one, POOL_SIZE
from gateway.terminal import handle_terminal
from gateway.store import store

setup_logging()
logger = logging.getLogger("abax.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: recover state from any previous crash
    await recover_state()

    # Clean orphan containers from previous runs
    removed = await collect_garbage(max_idle_seconds=0)
    if removed:
        logger.info("Startup cleanup: removed %d orphan containers", len(removed))

    # Start background tasks
    gc_task = asyncio.create_task(gc_loop())
    pool_task = asyncio.create_task(pool_loop())
    yield
    # Shutdown
    gc_task.cancel()
    pool_task.cancel()


app = FastAPI(title="Abax Gateway", version="0.2.0", lifespan=lifespan)

# Auth dependency applied to all protected routes via shorthand
Auth = Depends(verify_api_key)


# --- Request middleware: assign request_id, track metrics ---


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request_id_var.set(req_id)

    # Parse path segments once for sandbox_id extraction and metric normalization
    parts = request.url.path.split("/")
    is_sandbox_route = len(parts) >= 3 and parts[1] == "sandboxes"

    sandbox_id_var.set(parts[2] if is_sandbox_route else None)

    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start

    # Normalize path for cardinality control (replace sandbox IDs with placeholder)
    if is_sandbox_route:
        parts[2] = ":id"
    metric_path = "/".join(parts) if is_sandbox_route else request.url.path

    requests_total.labels(
        method=request.method,
        path=metric_path,
        status=response.status_code,
    ).inc()

    response.headers["X-Request-ID"] = req_id
    logger.info(
        "%s %s %d %.3fs",
        request.method,
        request.url.path,
        response.status_code,
        duration,
    )
    return response


# --- Health & Metrics (no auth) ---


@app.get("/metrics")
async def prometheus_metrics():
    body, content_type = metrics_response_bytes()
    return Response(content=body, media_type=content_type)


@app.get("/health", response_model=HealthResponse)
async def health():
    import docker as _docker

    docker_ok = False
    image_ok = False
    active = 0

    try:
        c = _docker.from_env()
        c.ping()
        docker_ok = True
    except Exception:
        pass

    if docker_ok:
        try:
            c.images.get(sandbox.SANDBOX_IMAGE)
            image_ok = True
        except Exception:
            pass
        try:
            containers = c.containers.list(
                filters={"label": f"{sandbox.LABEL_PREFIX}.managed=true"}
            )
            active = len(containers)
        except Exception:
            pass

    pool_size = 0
    try:
        pool_size = await warm_pool_count()
    except Exception:
        pass

    if docker_ok and image_ok:
        status = "ok"
    elif docker_ok:
        status = "degraded"
    else:
        status = "error"
    return HealthResponse(
        status=status,
        docker_connected=docker_ok,
        sandbox_image_ready=image_ok,
        active_sandboxes=active,
        warm_pool_size=pool_size,
    )


# --- Sandbox lifecycle ---


@app.post("/sandboxes", response_model=SandboxInfo)
async def create_sandbox(req: CreateSandboxRequest, _=Auth):
    try:
        # Drain a pool container (warms Docker cache) then create a managed one
        if POOL_SIZE > 0:
            await drain_one()
        info = await sandbox.create_sandbox(req.user_id)
        store.register(info.sandbox_id, req.user_id)
        await emit_event(info.sandbox_id, "sandbox.created", {"user_id": req.user_id})
        return info
    except sandbox.SandboxLimitExceeded as e:
        raise HTTPException(429, str(e))


@app.get("/sandboxes", response_model=list[SandboxInfo])
async def list_sandboxes(_=Auth):
    return await sandbox.list_sandboxes()


@app.get("/sandboxes/{sandbox_id}", response_model=SandboxInfo)
async def get_sandbox(sandbox_id: str, _=Auth):
    try:
        return await sandbox.get_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.post("/sandboxes/{sandbox_id}/stop", response_model=SandboxInfo)
async def stop_sandbox(sandbox_id: str, _=Auth):
    try:
        result = await sandbox.stop_sandbox(sandbox_id)
        await emit_event(sandbox_id, "sandbox.stopped")
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.post("/sandboxes/{sandbox_id}/pause", response_model=SandboxInfo)
async def pause_sandbox(sandbox_id: str, _=Auth):
    try:
        store.record_activity(sandbox_id)
        result = await sandbox.pause_sandbox(sandbox_id)
        await emit_event(sandbox_id, "sandbox.paused")
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except SandboxStateError as e:
        raise HTTPException(409, str(e))


@app.post("/sandboxes/{sandbox_id}/resume", response_model=SandboxInfo)
async def resume_sandbox(sandbox_id: str, _=Auth):
    try:
        result = await sandbox.resume_sandbox(sandbox_id)
        await emit_event(sandbox_id, "sandbox.resumed")
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except SandboxStateError as e:
        raise HTTPException(409, str(e))


@app.delete("/sandboxes/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str, _=Auth):
    try:
        await sandbox.destroy_sandbox(sandbox_id)
        store.unregister(sandbox_id)
        await emit_event(sandbox_id, "sandbox.destroyed")
    except NotFound:
        raise HTTPException(404, "sandbox not found")


# --- Command execution ---


@app.post("/sandboxes/{sandbox_id}/exec")
async def exec_command(sandbox_id: str, req: ExecRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        await emit_event(sandbox_id, "exec.started", {"command": req.command})
        result = await executor.exec_command(sandbox_id, req.command, req.timeout)
        await emit_event(sandbox_id, "exec.completed", {"command": req.command, "exit_code": result.exit_code})
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except asyncio.TimeoutError:
        await emit_event(sandbox_id, "exec.timeout", {"command": req.command})
        raise HTTPException(504, "command execution timed out")


@app.websocket("/sandboxes/{sandbox_id}/stream")
async def stream_command(websocket: WebSocket, sandbox_id: str):
    store.record_activity(sandbox_id)
    await executor.stream_command(sandbox_id, websocket)


# --- PTY Terminal ---


@app.websocket("/sandboxes/{sandbox_id}/terminal")
async def terminal(websocket: WebSocket, sandbox_id: str):
    store.record_activity(sandbox_id)
    await handle_terminal(websocket, sandbox_id)


# --- File operations ---


@app.get("/sandboxes/{sandbox_id}/files/{path:path}")
async def read_file(sandbox_id: str, path: str, _=Auth):
    try:
        store.record_activity(sandbox_id)
        content = await files.read_file(sandbox_id, f"/{path}")
        return FileContent(content=content, path=f"/{path}")
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.put("/sandboxes/{sandbox_id}/files/{path:path}")
async def write_file(sandbox_id: str, path: str, req: FileContent, _=Auth):
    try:
        store.record_activity(sandbox_id)
        await files.write_file(sandbox_id, f"/{path}", req.content)
        await emit_event(sandbox_id, "file.written", {"path": f"/{path}"})
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.get("/sandboxes/{sandbox_id}/files-url/{path:path}")
async def get_download_url(sandbox_id: str, path: str, _=Auth):
    store.record_activity(sandbox_id)
    token = files.generate_download_token(sandbox_id, f"/{path}")
    return {"url": f"/files/{token}"}


@app.get("/files/{token}")
async def download_file(token: str):
    try:
        sid, path = files.verify_download_token(token)
        data, filename = await files.read_file_bytes(sid, path)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as e:
        raise HTTPException(403, str(e))
    except NotFound:
        raise HTTPException(404, "sandbox not found")


# --- Directory listing ---


@app.get("/sandboxes/{sandbox_id}/ls/{path:path}", response_model=DirListing)
async def list_dir(sandbox_id: str, path: str, _=Auth):
    try:
        store.record_activity(sandbox_id)
        entries_raw = await files.list_dir(sandbox_id, f"/{path}")
        entries = [DirEntry(**e) for e in entries_raw]
        return DirListing(path=f"/{path}", entries=entries)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# --- Binary file upload ---


@app.put("/sandboxes/{sandbox_id}/files-bin/{path:path}")
async def write_file_binary(sandbox_id: str, path: str, req: BinaryFileContent, _=Auth):
    try:
        store.record_activity(sandbox_id)
        try:
            data = base64.b64decode(req.data_b64)
        except Exception:
            raise HTTPException(400, "invalid base64 data")
        await files.write_file_bytes(sandbox_id, f"/{path}", data)
        await emit_event(sandbox_id, "file.written", {"path": f"/{path}"})
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


# --- Browser automation ---


@app.post("/sandboxes/{sandbox_id}/browser/navigate")
async def browser_navigate(sandbox_id: str, req: BrowserNavigateRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        result = await browser.navigate(sandbox_id, req.url)
        await emit_event(sandbox_id, "browser.navigated", {"url": req.url})
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/sandboxes/{sandbox_id}/browser/screenshot")
async def browser_screenshot(sandbox_id: str, req: BrowserScreenshotRequest | None = None, _=Auth):
    try:
        store.record_activity(sandbox_id)
        full_page = (req or BrowserScreenshotRequest()).full_page
        return await browser.screenshot(sandbox_id, full_page)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/sandboxes/{sandbox_id}/browser/click")
async def browser_click(sandbox_id: str, req: BrowserClickRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.click(sandbox_id, req.selector)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/sandboxes/{sandbox_id}/browser/type")
async def browser_type(sandbox_id: str, req: BrowserTypeRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.type_text(sandbox_id, req.selector, req.text)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/sandboxes/{sandbox_id}/browser/content")
async def browser_content(sandbox_id: str, mode: str = "text", _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.get_content(sandbox_id, mode)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# --- SSE Events ---


@app.get("/sandboxes/{sandbox_id}/events")
async def sandbox_events(sandbox_id: str, _=Auth):
    return StreamingResponse(
        sse_stream(sandbox_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
