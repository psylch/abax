# Infra Layer Separation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Separate the Infra layer from Agent orchestration into a clean monorepo structure, making the gateway a pure sandbox runtime.

**Architecture:** Move `gateway/` to `infra/` with `api/` (routes) and `core/` (business logic) split. Agent-related code (session, chat, LLM proxy, Tier routing) moves to `agent/legacy/`. Daemon drops `/agent/turn`, gains persistent bash sessions. All gateway operations unified through daemon HTTP (no more direct docker exec for file ops).

**Tech Stack:** Python 3.12, FastAPI, Docker SDK, SQLite, pytest, httpx

---

### Task 1: Create directory structure

**Files:**
- Create: `infra/` directory tree
- Create: `infra/__init__.py`, `infra/api/__init__.py`, `infra/core/__init__.py`

**Step 1: Create all directories and init files**

```bash
mkdir -p infra/api infra/core
touch infra/__init__.py infra/api/__init__.py infra/core/__init__.py
mkdir -p agent/legacy
```

**Step 2: Verify structure**

Run: `find infra -type f | sort`
Expected:
```
infra/__init__.py
infra/api/__init__.py
infra/core/__init__.py
```

**Step 3: Commit**

```bash
git add infra/ agent/legacy/
git commit -m "chore: create infra/ and agent/legacy/ directory structure"
```

---

### Task 2: Move core business logic to infra/core/

Move the pure business logic files from `gateway/` to `infra/core/`, updating internal imports.

**Files:**
- Move: `gateway/sandbox.py` → `infra/core/sandbox.py`
- Move: `gateway/executor.py` → `infra/core/executor.py`
- Move: `gateway/files.py` → `infra/core/files.py`
- Move: `gateway/browser.py` → `infra/core/browser.py`
- Move: `gateway/terminal.py` → `infra/core/terminal.py`
- Move: `gateway/gc.py` → `infra/core/gc.py`
- Move: `gateway/pool.py` → `infra/core/pool.py`
- Move: `gateway/recovery.py` → `infra/core/recovery.py`
- Move: `gateway/store.py` → `infra/core/store.py`
- Move: `gateway/daemon.py` → `infra/core/daemon.py`

**Step 1: Copy files with updated imports**

For each file, the import prefix changes from `gateway.` to `infra.core.` for internal references, and `gateway.models` → `infra.models`.

Key import changes per file:
- `infra/core/sandbox.py`: `from gateway.models import SandboxInfo` → `from infra.models import SandboxInfo`
- `infra/core/executor.py`: `from gateway.daemon import request_sync` → `from infra.core.daemon import request_sync`, `from gateway.models import ExecResult` → `from infra.models import ExecResult`, `from gateway.sandbox import get_container` → `from infra.core.sandbox import get_container`
- `infra/core/files.py`: `from gateway.daemon import request_sync` → `from infra.core.daemon import request_sync`, `from gateway.sandbox import get_container` → `from infra.core.sandbox import get_container`
- `infra/core/browser.py`: `from gateway.daemon import request_sync` → `from infra.core.daemon import request_sync`
- `infra/core/terminal.py`: `from gateway.sandbox import get_container` → `from infra.core.sandbox import get_container`
- `infra/core/gc.py`: `from gateway.store import store` → `from infra.core.store import store`
- `infra/core/recovery.py`: `from gateway.sandbox import LABEL_PREFIX, client` → `from infra.core.sandbox import LABEL_PREFIX, client`, `from gateway.store import store` → `from infra.core.store import store`
- `infra/core/pool.py`: `from gateway.sandbox import LABEL_PREFIX, RUNTIME, SANDBOX_IMAGE, client` → `from infra.core.sandbox import LABEL_PREFIX, RUNTIME, SANDBOX_IMAGE, client`
- `infra/core/daemon.py`: `from gateway.sandbox import get_container` → `from infra.core.sandbox import get_container`
- `infra/core/store.py`: No import changes needed (only uses stdlib)

**Step 2: Strip session/message code from store.py**

Remove from `infra/core/store.py`:
- The `sessions` and `messages` table creation in `_init_db()`
- The migration `ALTER TABLE sessions ADD COLUMN sandbox_id TEXT`
- All methods after `all_sandbox_ids()`: `create_session`, `get_session`, `list_sessions`, `bind_session_container`, `get_session_container`, `clear_session_container`, `save_message`, `load_history`

The resulting `infra/core/store.py` should only have: `_init_db()` (sandboxes table only), `_connect()`, `register()`, `record_activity()`, `unregister()`, `get_idle_sandboxes()`, `get_sandbox_meta()`, `all_sandbox_ids()`.

**Step 3: Strip `clear_session_container` calls from gc.py**

In `infra/core/gc.py`, remove all `store.clear_session_container(...)` calls (3 occurrences in Phase 2, Phase 3, and Phase 4 of `_collect`).

**Step 4: Run a quick import check**

```bash
cd /Users/lichihao/Workspaces/agentWorkspace/abax
python -c "from infra.core import sandbox, executor, files, browser, terminal, gc, pool, recovery, store, daemon; print('OK')"
```

Expected: `OK`

**Step 5: Commit**

```bash
git add infra/core/
git commit -m "refactor: move core business logic to infra/core/"
```

---

### Task 3: Move shared modules to infra/

Move auth, events, metrics, logging_config, and models to `infra/`.

**Files:**
- Move: `gateway/auth.py` → `infra/auth.py`
- Move: `gateway/events.py` → `infra/events.py`
- Move: `gateway/metrics.py` → `infra/metrics.py`
- Move: `gateway/logging_config.py` → `infra/logging_config.py`
- Create: `infra/models.py` (Infra-only models, stripped of Session/Chat/LLM models)

**Step 1: Copy auth, events, metrics, logging_config as-is** (no import changes needed — they only use stdlib/third-party)

**Step 2: Create infra/models.py**

Include only these models from `gateway/models.py` (lines 1-86):
- `CreateSandboxRequest` — BUT add optional `volumes` field: `volumes: dict[str, str] | None = None`
- `SandboxInfo`
- `ExecRequest`
- `ExecResult`
- `FileContent`
- `HealthResponse`
- `DirEntry`
- `DirListing`
- `BinaryFileContent`
- `BrowserNavigateRequest`
- `BrowserScreenshotRequest`
- `BrowserClickRequest`
- `BrowserTypeRequest`
- `FileBatchOp`
- `FileBatchRequest`

Do NOT include: `CreateSessionRequest`, `SessionInfo`, `MessageRequest`, `MessageInfo`, `SessionHistoryResponse`, `ChatRequest`, `ChatResponse`, `LLMProxyRequest`.

**Step 3: Verify imports**

```bash
python -c "from infra.models import CreateSandboxRequest, SandboxInfo, ExecRequest; print('OK')"
```

**Step 4: Commit**

```bash
git add infra/auth.py infra/events.py infra/metrics.py infra/logging_config.py infra/models.py
git commit -m "refactor: move shared modules and Infra-only models to infra/"
```

---

### Task 4: Create infra/api/ route modules

Split `gateway/main.py` routes into focused route modules under `infra/api/`.

**Files:**
- Create: `infra/api/sandbox.py` — sandbox CRUD routes (create, list, get, stop, pause, resume, destroy)
- Create: `infra/api/exec.py` — exec, stream, terminal routes
- Create: `infra/api/files.py` — file read/write/list/batch/binary/download routes
- Create: `infra/api/browser.py` — browser navigate/screenshot/click/type/content routes
- Create: `infra/api/main.py` — FastAPI app, lifespan, middleware, health, metrics, events, include routers

**Step 1: Create infra/api/sandbox.py**

```python
"""Sandbox lifecycle routes."""
from fastapi import APIRouter, Depends, HTTPException
from docker.errors import NotFound

from infra.auth import verify_api_key
from infra.core import sandbox
from infra.core.sandbox import SandboxStateError
from infra.core.store import store
from infra.core.pool import POOL_SIZE, drain_one
from infra.events import publish as emit_event
from infra.models import CreateSandboxRequest, SandboxInfo

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
Auth = Depends(verify_api_key)


@router.post("", response_model=SandboxInfo)
async def create_sandbox(req: CreateSandboxRequest, _=Auth):
    try:
        if POOL_SIZE > 0:
            await drain_one()
        info = await sandbox.create_sandbox(req.user_id)
        store.register(info.sandbox_id, req.user_id)
        await emit_event(info.sandbox_id, "sandbox.created", {"user_id": req.user_id})
        return info
    except sandbox.SandboxLimitExceeded as e:
        raise HTTPException(429, str(e))


@router.get("", response_model=list[SandboxInfo])
async def list_sandboxes(_=Auth):
    return await sandbox.list_sandboxes()


@router.get("/{sandbox_id}", response_model=SandboxInfo)
async def get_sandbox(sandbox_id: str, _=Auth):
    try:
        return await sandbox.get_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@router.post("/{sandbox_id}/stop", response_model=SandboxInfo)
async def stop_sandbox(sandbox_id: str, _=Auth):
    try:
        result = await sandbox.stop_sandbox(sandbox_id)
        await emit_event(sandbox_id, "sandbox.stopped")
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@router.post("/{sandbox_id}/pause", response_model=SandboxInfo)
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


@router.post("/{sandbox_id}/resume", response_model=SandboxInfo)
async def resume_sandbox(sandbox_id: str, _=Auth):
    try:
        result = await sandbox.resume_sandbox(sandbox_id)
        await emit_event(sandbox_id, "sandbox.resumed")
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except SandboxStateError as e:
        raise HTTPException(409, str(e))


@router.delete("/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str, _=Auth):
    try:
        await sandbox.destroy_sandbox(sandbox_id)
        store.unregister(sandbox_id)
        await emit_event(sandbox_id, "sandbox.destroyed")
    except NotFound:
        raise HTTPException(404, "sandbox not found")
```

**Step 2: Create infra/api/exec.py**

```python
"""Command execution routes."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from docker.errors import NotFound

from infra.auth import verify_api_key
from infra.core import executor
from infra.core.store import store
from infra.core.terminal import handle_terminal
from infra.events import publish as emit_event
from infra.models import ExecRequest

router = APIRouter(prefix="/sandboxes", tags=["exec"])
Auth = Depends(verify_api_key)


@router.post("/{sandbox_id}/exec")
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


@router.websocket("/{sandbox_id}/stream")
async def stream_command(websocket: WebSocket, sandbox_id: str):
    store.record_activity(sandbox_id)
    await executor.stream_command(sandbox_id, websocket)


@router.websocket("/{sandbox_id}/terminal")
async def terminal(websocket: WebSocket, sandbox_id: str):
    store.record_activity(sandbox_id)
    await handle_terminal(websocket, sandbox_id)
```

**Step 3: Create infra/api/files.py**

```python
"""File operation routes."""
import base64

from fastapi import APIRouter, Depends, HTTPException, Response
from docker.errors import NotFound

from infra.auth import verify_api_key
from infra.core import files
from infra.core.store import store
from infra.events import publish as emit_event
from infra.models import (
    BinaryFileContent, DirEntry, DirListing, FileBatchRequest, FileContent,
)

router = APIRouter(tags=["files"])
Auth = Depends(verify_api_key)


@router.get("/sandboxes/{sandbox_id}/files/{path:path}")
async def read_file(sandbox_id: str, path: str, _=Auth):
    try:
        store.record_activity(sandbox_id)
        content = await files.read_file(sandbox_id, f"/{path}")
        return FileContent(content=content, path=f"/{path}")
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.put("/sandboxes/{sandbox_id}/files/{path:path}")
async def write_file(sandbox_id: str, path: str, req: FileContent, _=Auth):
    try:
        store.record_activity(sandbox_id)
        await files.write_file(sandbox_id, f"/{path}", req.content)
        await emit_event(sandbox_id, "file.written", {"path": f"/{path}"})
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@router.get("/sandboxes/{sandbox_id}/files-url/{path:path}")
async def get_download_url(sandbox_id: str, path: str, _=Auth):
    store.record_activity(sandbox_id)
    token = files.generate_download_token(sandbox_id, f"/{path}")
    return {"url": f"/files/{token}"}


@router.get("/files/{token}")
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


@router.get("/sandboxes/{sandbox_id}/ls/{path:path}", response_model=DirListing)
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


@router.post("/sandboxes/{sandbox_id}/files-batch")
async def batch_file_ops(sandbox_id: str, req: FileBatchRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        ops = [op.model_dump() for op in req.operations]
        results = await files.batch_file_ops(sandbox_id, ops)
        return {"results": results}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@router.put("/sandboxes/{sandbox_id}/files-bin/{path:path}")
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
```

**Step 4: Create infra/api/browser.py**

```python
"""Browser automation routes."""
from fastapi import APIRouter, Depends, HTTPException
from docker.errors import NotFound

from infra.auth import verify_api_key
from infra.core import browser
from infra.core.store import store
from infra.events import publish as emit_event
from infra.models import (
    BrowserClickRequest, BrowserNavigateRequest,
    BrowserScreenshotRequest, BrowserTypeRequest,
)

router = APIRouter(prefix="/sandboxes/{sandbox_id}/browser", tags=["browser"])
Auth = Depends(verify_api_key)


@router.post("/navigate")
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


@router.post("/screenshot")
async def browser_screenshot(sandbox_id: str, req: BrowserScreenshotRequest | None = None, _=Auth):
    try:
        store.record_activity(sandbox_id)
        full_page = (req or BrowserScreenshotRequest()).full_page
        return await browser.screenshot(sandbox_id, full_page)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/click")
async def browser_click(sandbox_id: str, req: BrowserClickRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.click(sandbox_id, req.selector)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/type")
async def browser_type(sandbox_id: str, req: BrowserTypeRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.type_text(sandbox_id, req.selector, req.text)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/content")
async def browser_content(sandbox_id: str, mode: str = "text", _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.get_content(sandbox_id, mode)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))
```

**Step 5: Create infra/api/main.py**

The main FastAPI app that composes all routers. Includes lifespan (recovery + GC + pool), observability middleware, health, metrics, events.

```python
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
```

**Step 6: Verify the app starts**

```bash
python -c "from infra.api.main import app; print(f'Routes: {len(app.routes)}')"
```

**Step 7: Commit**

```bash
git add infra/api/
git commit -m "refactor: create infra/api/ route modules with router split"
```

---

### Task 5: Move agent-related code to agent/legacy/

**Files:**
- Move: `gateway/agent.py` → `agent/legacy/agent.py`
- Move: `gateway/llm_proxy.py` → `agent/legacy/llm_proxy.py`
- Move: `gateway/context.py` → `agent/legacy/context.py`
- Create: `agent/legacy/session_store.py` (session/message methods extracted from store.py)
- Create: `agent/legacy/models.py` (Session/Chat/LLM models from gateway/models.py)
- Create: `agent/legacy/__init__.py`
- Create: `agent/legacy/README.md`

**Step 1: Create agent/legacy/__init__.py**

Empty file.

**Step 2: Create agent/legacy/models.py**

Extract from `gateway/models.py` lines 88-148:
```python
from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    title: str | None = None


class SessionInfo(BaseModel):
    session_id: str
    user_id: str
    title: str | None = None
    sandbox_id: str | None = None
    created_at: float
    last_active_at: float


class MessageRequest(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=10 * 1024 * 1024)
    tool_calls: str | None = None
    tool_results: str | None = None


class MessageInfo(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    tool_calls: str | None = None
    tool_results: str | None = None
    created_at: float


class SessionHistoryResponse(BaseModel):
    session_id: str
    messages: list[MessageInfo]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10 * 1024 * 1024)


class ChatResponse(BaseModel):
    response: str
    tier: str
    sandbox_id: str | None = None
    tool_calls_count: int = 0


class LLMProxyRequest(BaseModel):
    model: str
    max_tokens: int = 4096
    system: str | None = None
    messages: list[dict]
    tools: list[dict] | None = None
    stream: bool = False
```

**Step 3: Create agent/legacy/session_store.py**

Extract session/message methods from `gateway/store.py` (lines 141-300). This is a standalone module — it creates its own SQLite connection.

**Step 4: Copy agent.py, llm_proxy.py, context.py as-is**

These files keep their original imports (they reference `gateway.*`). They are legacy reference code, not expected to run.

**Step 5: Create agent/legacy/README.md**

```markdown
# Agent Legacy Code

This directory contains agent orchestration code extracted from the gateway
during the Infra layer separation refactor (2026-02-24).

**Status:** Reference code only. Not actively used.

**Purpose:** Preserved as reference for when the Agent layer is rebuilt
using a proper framework (e.g., Claude Agent SDK).

## Key files

- `agent.py` — Tier 1/2/3 routing, sandbox lifecycle for chat
- `llm_proxy.py` — Anthropic API proxy with key injection
- `context.py` — Host-side user context file reader
- `session_store.py` — Session/message SQLite persistence
- `models.py` — Pydantic models for sessions, chat, LLM proxy

## What to reuse

- Tool definitions in `agent.py` (TOOL_DEFINITIONS) — the interface between agent and sandbox
- LLM proxy pattern — key injection without exposing to containers
- Tier routing concept — avoid containers for pure-chat interactions
```

**Step 6: Commit**

```bash
git add agent/legacy/
git commit -m "refactor: move agent orchestration code to agent/legacy/"
```

---

### Task 6: Update tests

**Files:**
- Create: `tests/infra/` directory
- Create: `tests/infra/__init__.py`
- Create: `tests/infra/conftest.py` (updated from `tests/conftest.py`)
- Move: Infra tests to `tests/infra/`
- Move: Agent tests to `tests/agent/` (as reference)

**Step 1: Create test directories**

```bash
mkdir -p tests/infra tests/agent
touch tests/infra/__init__.py tests/agent/__init__.py
```

**Step 2: Create tests/infra/conftest.py**

Same as current `tests/conftest.py` but with `from infra.api.main import app` instead of `from gateway.main import app`.

**Step 3: Move infra tests**

These tests stay (with updated imports — `from infra.api.main import app` in conftest):
- `test_gateway.py` → `tests/infra/test_gateway.py`
- `test_lifecycle.py` → `tests/infra/test_lifecycle.py`
- `test_exec_timeout.py` → `tests/infra/test_exec_timeout.py`
- `test_files_extended.py` → `tests/infra/test_files_extended.py`
- `test_gc.py` → `tests/infra/test_gc.py`
- `test_health.py` → `tests/infra/test_health.py`
- `test_pause.py` → `tests/infra/test_pause.py`
- `test_pool.py` → `tests/infra/test_pool.py`
- `test_recovery.py` → `tests/infra/test_recovery.py`
- `test_events.py` → `tests/infra/test_events.py`
- `test_metrics.py` → `tests/infra/test_metrics.py`
- `test_auth.py` → `tests/infra/test_auth.py`
- `test_store.py` → `tests/infra/test_store.py`
- `test_limits.py` → `tests/infra/test_limits.py`
- `test_async.py` → `tests/infra/test_async.py`
- `test_daemon.py` → `tests/infra/test_daemon.py`
- `test_sdk.py` → `tests/infra/test_sdk.py`
- `test_stress.py` → `tests/infra/test_stress.py`
- `test_stress_vps.py` → `tests/infra/test_stress_vps.py`
- `test_stress_multitenant.py` → `tests/infra/test_stress_multitenant.py`

**Step 4: Move agent tests to tests/agent/ as reference**

- `test_session.py` → `tests/agent/test_session.py`
- `test_agent_turn.py` → `tests/agent/test_agent_turn.py`
- `test_e2e.py` → `tests/agent/test_e2e.py`

**Step 5: Update imports in all moved test files**

Each test file that imports from `gateway.*` needs updating to `infra.*`. The key change is in the conftest fixture.

For test files that import models or store directly, update:
- `from gateway.store import SandboxStore` → `from infra.core.store import SandboxStore`
- `from gateway.models import ...` → `from infra.models import ...`
- `from gateway.auth import ...` → `from infra.auth import ...`
- `from gateway.events import ...` → `from infra.events import ...`

**Step 6: Run infra tests**

```bash
ABAX_POOL_SIZE=0 pytest tests/infra/ -v --timeout=120 2>&1 | tail -30
```

Expected: All infra tests pass (approximately 150+ tests).

**Step 7: Commit**

```bash
git add tests/
git commit -m "refactor: reorganize tests into infra/ and agent/ directories"
```

---

### Task 7: Clean up daemon (remove /agent/turn)

**Files:**
- Modify: `sandbox-image/sandbox_server.py`

**Step 1: Remove agent turn code from sandbox_server.py**

Remove everything from line 330 onwards:
- `AgentTurnRequest` model class
- `_format_exec_result()` function
- `_local_tool_exec()` function
- `_call_llm()` function
- `@app.post("/agent/turn")` endpoint

Keep everything else (health, exec, files, browser).

**Step 2: Verify daemon still works**

```bash
docker build -t abax-sandbox ./sandbox-image
```

Expected: Build succeeds.

**Step 3: Run a quick smoke test**

```bash
ABAX_POOL_SIZE=0 pytest tests/infra/test_gateway.py -v --timeout=60 -x
```

**Step 4: Commit**

```bash
git add sandbox-image/sandbox_server.py
git commit -m "refactor: remove /agent/turn from sandbox daemon"
```

---

### Task 8: Add persistent bash session to daemon

**Files:**
- Modify: `sandbox-image/sandbox_server.py` — add bash session endpoints
- Modify: `infra/core/daemon.py` — add bash helper functions
- Create: `infra/api/bash.py` — Gateway bash routes (optional, can be in exec.py)
- Modify: `infra/api/main.py` — include bash router
- Create: `tests/infra/test_bash.py` — tests

**Step 1: Write the failing test**

```python
# tests/infra/test_bash.py
import pytest


@pytest.mark.asyncio
async def test_create_bash_session(client, sandbox_id):
    """Create a persistent bash session."""
    r = await client.post(f"/sandboxes/{sandbox_id}/bash")
    assert r.status_code == 200
    data = r.json()
    assert "bash_id" in data
    # Cleanup
    await client.delete(f"/sandboxes/{sandbox_id}/bash/{data['bash_id']}")


@pytest.mark.asyncio
async def test_bash_run_command(client, sandbox_id):
    """Run a command in a persistent bash session."""
    # Create session
    r = await client.post(f"/sandboxes/{sandbox_id}/bash")
    assert r.status_code == 200
    bash_id = r.json()["bash_id"]

    # Run command
    r = await client.post(
        f"/sandboxes/{sandbox_id}/bash/{bash_id}/run",
        json={"command": "echo hello"}
    )
    assert r.status_code == 200
    assert "hello" in r.json()["stdout"]

    # Cleanup
    await client.delete(f"/sandboxes/{sandbox_id}/bash/{bash_id}")


@pytest.mark.asyncio
async def test_bash_state_persistence(client, sandbox_id):
    """Verify that bash session maintains state between commands."""
    r = await client.post(f"/sandboxes/{sandbox_id}/bash")
    bash_id = r.json()["bash_id"]

    # Set a variable
    r = await client.post(
        f"/sandboxes/{sandbox_id}/bash/{bash_id}/run",
        json={"command": "export MY_VAR=hello_world"}
    )
    assert r.status_code == 200

    # Read it back
    r = await client.post(
        f"/sandboxes/{sandbox_id}/bash/{bash_id}/run",
        json={"command": "echo $MY_VAR"}
    )
    assert r.status_code == 200
    assert "hello_world" in r.json()["stdout"]

    # Change directory
    r = await client.post(
        f"/sandboxes/{sandbox_id}/bash/{bash_id}/run",
        json={"command": "cd /tmp && pwd"}
    )
    assert "tmp" in r.json()["stdout"]

    # Verify we're still in /tmp
    r = await client.post(
        f"/sandboxes/{sandbox_id}/bash/{bash_id}/run",
        json={"command": "pwd"}
    )
    assert "tmp" in r.json()["stdout"]

    await client.delete(f"/sandboxes/{sandbox_id}/bash/{bash_id}")


@pytest.mark.asyncio
async def test_bash_delete_session(client, sandbox_id):
    """Delete a bash session."""
    r = await client.post(f"/sandboxes/{sandbox_id}/bash")
    bash_id = r.json()["bash_id"]

    r = await client.delete(f"/sandboxes/{sandbox_id}/bash/{bash_id}")
    assert r.status_code == 200

    # Running a command on deleted session should fail
    r = await client.post(
        f"/sandboxes/{sandbox_id}/bash/{bash_id}/run",
        json={"command": "echo test"}
    )
    assert r.status_code == 404
```

**Step 2: Run tests to verify they fail**

```bash
ABAX_POOL_SIZE=0 pytest tests/infra/test_bash.py -v --timeout=60
```

Expected: FAIL — endpoints don't exist yet.

**Step 3: Implement bash sessions in daemon**

Add to `sandbox-image/sandbox_server.py`:

```python
import uuid as _uuid

# Persistent bash sessions: bash_id -> subprocess
_bash_sessions: dict[str, asyncio.subprocess.Process] = {}
_bash_lock = asyncio.Lock()


class BashRunRequest(BaseModel):
    command: str
    timeout: int = 30


@app.post("/bash/create")
async def create_bash():
    """Create a persistent bash process."""
    bash_id = _uuid.uuid4().hex[:12]
    proc = await asyncio.create_subprocess_exec(
        "bash", "--norc", "--noprofile", "-i",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**dict(__import__("os").environ), "PS1": ""},
    )
    _bash_sessions[bash_id] = proc
    return {"bash_id": bash_id}


@app.post("/bash/{bash_id}/run")
async def bash_run(bash_id: str, req: BashRunRequest):
    """Run a command in an existing bash session."""
    proc = _bash_sessions.get(bash_id)
    if proc is None or proc.returncode is not None:
        return {"error": f"bash session {bash_id} not found", "status": 404}

    # Use a unique delimiter to detect end of output
    delimiter = f"__ABAX_END_{_uuid.uuid4().hex[:8]}__"
    full_cmd = f"{req.command}\necho {delimiter} $?\n"

    proc.stdin.write(full_cmd.encode())
    await proc.stdin.drain()

    # Read until we see the delimiter
    output_lines = []
    try:
        async with asyncio.timeout(req.timeout):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                if decoded.startswith(delimiter):
                    # Extract exit code from delimiter line
                    parts = decoded.split()
                    exit_code = int(parts[1]) if len(parts) > 1 else 0
                    break
                output_lines.append(decoded)
            else:
                exit_code = -1
    except TimeoutError:
        return {"stdout": "\n".join(output_lines), "stderr": "command timed out", "exit_code": 124}

    return {"stdout": "\n".join(output_lines), "stderr": "", "exit_code": exit_code}


@app.delete("/bash/{bash_id}")
async def delete_bash(bash_id: str):
    """Close a persistent bash session."""
    proc = _bash_sessions.pop(bash_id, None)
    if proc is None:
        return {"error": f"bash session {bash_id} not found", "status": 404}
    proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    return {"ok": True}
```

**Step 4: Add Gateway routes for bash**

Add bash routes in `infra/api/exec.py` (or create `infra/api/bash.py`), proxying to the daemon's `/bash/*` endpoints via `request_sync`.

**Step 5: Rebuild image and run tests**

```bash
docker build -t abax-sandbox ./sandbox-image
ABAX_POOL_SIZE=0 pytest tests/infra/test_bash.py -v --timeout=60
```

Expected: All 4 tests PASS.

**Step 6: Commit**

```bash
git add sandbox-image/sandbox_server.py infra/api/ tests/infra/test_bash.py
git commit -m "feat: add persistent bash session to daemon and gateway"
```

---

### Task 9: Update Makefile, conftest, and SDK imports

**Files:**
- Modify: `Makefile` — update `gateway` target to use `infra.api.main:app`
- Modify: `sdk/sandbox.py` — verify SDK still works (it talks to HTTP endpoints, not Python imports, so likely no changes)
- Remove: `tests/conftest.py` — replaced by `tests/infra/conftest.py`

**Step 1: Update Makefile**

Change:
```makefile
gateway: image
	uvicorn gateway.main:app --reload --port 8000
```
To:
```makefile
gateway: image
	uvicorn infra.api.main:app --reload --port 8000
```

**Step 2: Run full test suite**

```bash
ABAX_POOL_SIZE=0 pytest tests/infra/ -v --timeout=120 2>&1 | tail -40
```

Expected: All infra tests pass.

**Step 3: Commit**

```bash
git add Makefile tests/
git commit -m "chore: update Makefile and test configuration for infra/ structure"
```

---

### Task 10: Clean up old gateway/ directory

**Files:**
- Remove: `gateway/` directory (all code has been moved)

**Step 1: Verify nothing still imports from gateway**

```bash
grep -r "from gateway\." infra/ tests/infra/ sdk/ sandbox-image/ Makefile 2>/dev/null || echo "CLEAN"
```

Expected: `CLEAN`

**Step 2: Remove old gateway directory**

```bash
rm -rf gateway/
```

**Step 3: Final full test run**

```bash
ABAX_POOL_SIZE=0 pytest tests/infra/ -v --timeout=120
```

Expected: All tests pass.

**Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove old gateway/ directory — all code migrated to infra/"
```

---

### Task 11: Update documentation

**Files:**
- Modify: `docs/infra-status-report.md`
- Modify: `docs/architecture-evolution.md`

**Step 1: Update infra-status-report.md**

Add a new version section (v5) documenting:
- Directory restructuring: `gateway/` → `infra/` with `api/` + `core/` split
- Agent code separated to `agent/legacy/`
- Daemon cleaned: `/agent/turn` removed, persistent bash added
- Store cleaned: session/message tables removed
- Models split: Infra-only vs Agent-only
- Test count and structure

**Step 2: Update architecture-evolution.md**

Add section documenting the layer separation and the new clean boundaries.

**Step 3: Commit**

```bash
git add docs/
git commit -m "docs: update status report and architecture for infra layer separation"
```

---

## Execution Parallelism

Tasks that can run in parallel (no dependencies):
- **Task 2 + Task 3** — core/ modules and shared modules can be created simultaneously
- **Task 5** — independent of Tasks 2-4 (just copies legacy code)
- **Task 7 + Task 8** — daemon changes are independent

Sequential dependencies:
- Tasks 2, 3 → Task 4 (routes import from core/ and shared modules)
- Tasks 4, 5 → Task 6 (tests need the new app and structure)
- Task 6 → Task 9 (Makefile update after tests are reorganized)
- Task 9 → Task 10 (verify before deleting old code)
- Task 10 → Task 11 (docs after everything is done)
