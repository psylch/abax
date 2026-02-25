"""Abax Infra — Sandbox Runtime API."""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from starlette.responses import StreamingResponse

from infra.api import sandbox as sandbox_routes
from infra.api import exec as exec_routes
from infra.api import files as files_routes
from infra.api import browser as browser_routes
from infra.api import bash as bash_routes
from infra.auth import verify_api_key
from infra.core.gc import gc_loop, collect_garbage
from infra.core.pool import pool_loop, warm_pool_count, POOL_SIZE
from infra.core.recovery import recover_state
from infra.core.store import store
from infra.core import sandbox
from infra.events import sse_stream
from infra.logging_config import setup_logging, request_id_var, sandbox_id_var
from infra.metrics import metrics_response_bytes, requests_total
from infra.models import HealthResponse

setup_logging()
logger = logging.getLogger("abax.infra")

Auth = Depends(verify_api_key)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await recover_state()
    removed = await collect_garbage(max_idle_seconds=0)
    if removed:
        logger.info("Startup cleanup: removed %d orphan containers", len(removed))
    gc_task = asyncio.create_task(gc_loop())
    pool_task = asyncio.create_task(pool_loop())
    yield
    gc_task.cancel()
    pool_task.cancel()


app = FastAPI(title="Abax Infra", version="0.3.0", lifespan=lifespan)

# Include route modules
app.include_router(sandbox_routes.router)
app.include_router(exec_routes.router)
app.include_router(files_routes.router)
app.include_router(browser_routes.router)
app.include_router(bash_routes.router)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request_id_var.set(req_id)
    parts = request.url.path.split("/")
    is_sandbox_route = len(parts) >= 3 and parts[1] == "sandboxes"
    sandbox_id_var.set(parts[2] if is_sandbox_route else None)
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start
    if is_sandbox_route:
        parts[2] = ":id"
    metric_path = "/".join(parts) if is_sandbox_route else request.url.path
    requests_total.labels(method=request.method, path=metric_path, status=response.status_code).inc()
    response.headers["X-Request-ID"] = req_id
    logger.info("%s %s %d %.3fs", request.method, request.url.path, response.status_code, duration)
    return response


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
            containers = c.containers.list(filters={"label": f"{sandbox.LABEL_PREFIX}.managed=true"})
            active = len(containers)
        except Exception:
            pass
    pool_size = 0
    try:
        pool_size = await warm_pool_count()
    except Exception:
        pass
    status = "ok" if docker_ok and image_ok else ("degraded" if docker_ok else "error")
    return HealthResponse(
        status=status, docker_connected=docker_ok, sandbox_image_ready=image_ok,
        active_sandboxes=active, warm_pool_size=pool_size,
    )


@app.get("/sandboxes/{sandbox_id}/events")
async def sandbox_events(sandbox_id: str, _=Auth):
    return StreamingResponse(
        sse_stream(sandbox_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
