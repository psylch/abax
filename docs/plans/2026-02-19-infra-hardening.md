# Infra/Gateway 层加固 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 Infra/Gateway 层做到本地开发自治——一键启动、自动清理、异步不阻塞、测试全自动。

**Architecture:** Gateway 保持 FastAPI 单进程架构，阻塞的 Docker SDK 调用通过 `asyncio.to_thread` 推到线程池。容器 GC 通过 FastAPI lifespan 启动后台任务，定时扫描 idle 容器。docker-compose 编排 Gateway + 沙箱镜像构建。

**Tech Stack:** FastAPI, Docker SDK (python-docker), docker-compose, Makefile, pytest

---

## 并行分组

以下 3 个 Task 可以 **并行执行**（互不修改同一文件）：

| Task | 涉及文件 | 独立性 |
|------|----------|--------|
| Task 1: docker-compose + Makefile | 新文件 `docker-compose.yml`, `Makefile` | ✅ 纯新文件 |
| Task 2: Gateway 异步化 | 改 `gateway/sandbox.py`, `gateway/executor.py`, `gateway/files.py` | ✅ 只改内部实现 |
| Task 3: 健康检查增强 | 改 `gateway/models.py` | ✅ 只加新 model |

以下 Task **依赖前面完成**：

| Task | 依赖 |
|------|------|
| Task 4: 容器 GC | 依赖 Task 2（需要异步化后的 sandbox 函数） |
| Task 5: 测试自动化 + 全量验证 | 依赖 Task 1-4 全部完成 |

---

## Task 1: docker-compose + Makefile（可并行）

**Files:**
- Create: `docker-compose.yml`
- Create: `Makefile`

**Step 1: 创建 docker-compose.yml**

```yaml
# docker-compose.yml
services:
  gateway:
    build:
      context: .
      dockerfile: Dockerfile.gateway
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /tmp/abax-persistent:/tmp/abax-persistent
    environment:
      - ABAX_SANDBOX_IMAGE=abax-sandbox
      - ABAX_SIGN_SECRET=${ABAX_SIGN_SECRET:-dev-secret-change-in-prod}
    depends_on:
      sandbox-image:
        condition: service_completed_successfully

  sandbox-image:
    image: abax-sandbox
    build:
      context: ./sandbox-image
      dockerfile: Dockerfile
    entrypoint: ["echo", "image built"]
```

**Step 2: 创建 Dockerfile.gateway**

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY gateway/ gateway/
EXPOSE 8000

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 3: 创建 Makefile**

```makefile
.PHONY: image gateway test clean dev

# 构建沙箱镜像
image:
	docker build -t abax-sandbox ./sandbox-image

# 启动 Gateway（本地开发，不走 Docker）
gateway: image
	uvicorn gateway.main:app --reload --port 8000

# 跑测试（自动确保镜像已构建）
test: image
	pytest tests/ -v

# 清理所有 abax 容器
clean:
	@echo "Stopping and removing all abax containers..."
	@docker ps -aq --filter "label=abax.managed=true" | xargs -r docker rm -f
	@echo "Done."

# docker-compose 一键启动
dev:
	docker compose up --build
```

**Step 4: 验证**

```bash
make image   # 构建沙箱镜像
make test    # 跑测试
make clean   # 清理容器
```

**Step 5: Commit**

```bash
git add docker-compose.yml Dockerfile.gateway Makefile
git commit -m "infra: add docker-compose, Makefile for one-click local dev"
```

---

## Task 2: Gateway 异步化（可并行）

**Files:**
- Modify: `gateway/sandbox.py`（全部函数）
- Modify: `gateway/executor.py:10-27`（exec_command）
- Modify: `gateway/files.py:14-49`（read_file, write_file, read_file_bytes）
- Modify: `gateway/main.py`（路由层去掉 redundant await，保持 async）

**核心思路：** 所有 Docker SDK 同步调用用 `asyncio.to_thread()` 包装。路由层调用方式从 `sandbox.create_sandbox(...)` 改为 `await sandbox.create_sandbox(...)`。

**Step 1: 写 sandbox.py 异步化的测试**

创建 `tests/test_async.py`：

```python
"""Test that gateway operations don't block the event loop."""
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from gateway.main import app

transport = ASGITransport(app=app)


@pytest.mark.asyncio
async def test_concurrent_health_during_exec():
    """Health check should respond even while exec is running."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create sandbox
        r = await client.post("/sandboxes", json={"user_id": "test-async"})
        assert r.status_code == 200
        sid = r.json()["sandbox_id"]

        try:
            # Fire a slow exec and a health check concurrently
            slow_exec = client.post(
                f"/sandboxes/{sid}/exec",
                json={"command": "sleep 2 && echo done", "timeout": 10},
            )
            health = client.get("/health")

            results = await asyncio.gather(health, slow_exec)
            health_r, exec_r = results

            assert health_r.status_code == 200
            assert exec_r.status_code == 200
            assert exec_r.json()["stdout"].strip() == "done"
        finally:
            await client.delete(f"/sandboxes/{sid}")
```

**Step 2: 跑测试确认失败**

```bash
pytest tests/test_async.py -v
```

预期：测试可能超时或 health 响应延迟（因为当前阻塞实现）。

**Step 3: 异步化 gateway/sandbox.py**

```python
import asyncio
import os
from pathlib import Path

import docker
from docker.errors import NotFound

from gateway.models import SandboxInfo

SANDBOX_IMAGE = os.getenv("ABAX_SANDBOX_IMAGE", "abax-sandbox")
PERSISTENT_ROOT = Path(os.getenv("ABAX_PERSISTENT_ROOT", "/tmp/abax-persistent"))
LABEL_PREFIX = "abax"

client = docker.from_env()


def _container_to_info(container) -> SandboxInfo:
    container.reload()
    return SandboxInfo(
        sandbox_id=container.id[:12],
        user_id=container.labels.get(f"{LABEL_PREFIX}.user_id", ""),
        status=container.status,
    )


def _create_sandbox_sync(user_id: str) -> SandboxInfo:
    user_data = PERSISTENT_ROOT / user_id
    user_data.mkdir(parents=True, exist_ok=True)

    container = client.containers.run(
        SANDBOX_IMAGE,
        detach=True,
        labels={
            f"{LABEL_PREFIX}.user_id": user_id,
            f"{LABEL_PREFIX}.managed": "true",
        },
        volumes={
            str(user_data): {"bind": "/data", "mode": "rw"},
        },
        mem_limit="512m",
        cpu_quota=50000,
        cpu_period=100000,
    )
    return _container_to_info(container)


async def create_sandbox(user_id: str) -> SandboxInfo:
    return await asyncio.to_thread(_create_sandbox_sync, user_id)


async def get_sandbox(sandbox_id: str) -> SandboxInfo:
    return await asyncio.to_thread(
        lambda: _container_to_info(client.containers.get(sandbox_id))
    )


async def list_sandboxes() -> list[SandboxInfo]:
    def _list():
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.managed=true"},
        )
        return [_container_to_info(c) for c in containers]
    return await asyncio.to_thread(_list)


async def stop_sandbox(sandbox_id: str) -> SandboxInfo:
    def _stop():
        container = client.containers.get(sandbox_id)
        container.stop(timeout=5)
        return _container_to_info(container)
    return await asyncio.to_thread(_stop)


async def destroy_sandbox(sandbox_id: str) -> None:
    def _destroy():
        container = client.containers.get(sandbox_id)
        container.remove(force=True)
    await asyncio.to_thread(_destroy)


def get_container(sandbox_id: str):
    """Get the raw docker container object for exec operations."""
    return client.containers.get(sandbox_id)
```

**Step 4: 异步化 gateway/executor.py**

```python
import asyncio
import json
import time

from fastapi import WebSocket, WebSocketDisconnect

from gateway.models import ExecResult
from gateway.sandbox import get_container


def _exec_sync(sandbox_id: str, command: str) -> ExecResult:
    container = get_container(sandbox_id)
    start = time.monotonic()
    exit_code, output = container.exec_run(
        ["bash", "-c", command],
        demux=True,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    stdout = output[0].decode("utf-8", errors="replace") if output[0] else ""
    stderr = output[1].decode("utf-8", errors="replace") if output[1] else ""

    return ExecResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


async def exec_command(sandbox_id: str, command: str, timeout: int = 30) -> ExecResult:
    return await asyncio.to_thread(_exec_sync, sandbox_id, command)


async def stream_command(sandbox_id: str, websocket: WebSocket):
    """Stream stays as-is — it already yields to the event loop via await websocket.send_json."""
    await websocket.accept()

    try:
        msg = await websocket.receive_text()
        data = json.loads(msg)
        command = data.get("command", "")

        if not command:
            await websocket.send_json({"type": "error", "data": "empty command"})
            await websocket.close()
            return

        container = get_container(sandbox_id)

        exec_instance = container.client.api.exec_create(
            container.id,
            ["bash", "-c", command],
            stdout=True,
            stderr=True,
        )

        # Run the blocking iterator in a thread, push chunks to a queue
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _stream():
            output = container.client.api.exec_start(exec_instance["Id"], stream=True)
            for chunk in output:
                text = chunk.decode("utf-8", errors="replace")
                queue.put_nowait(text)
            queue.put_nowait(None)  # sentinel

        stream_task = asyncio.get_event_loop().run_in_executor(None, _stream)

        while True:
            text = await queue.get()
            if text is None:
                break
            await websocket.send_json({"type": "stdout", "data": text})

        await stream_task  # ensure thread is done

        inspect = await asyncio.to_thread(
            container.client.api.exec_inspect, exec_instance["Id"]
        )
        exit_code = inspect.get("ExitCode", -1)
        await websocket.send_json({"type": "exit", "data": str(exit_code)})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
```

**Step 5: 异步化 gateway/files.py**

```python
import asyncio
import hashlib
import hmac
import io
import os
import tarfile
import time

from gateway.sandbox import get_container

SIGN_SECRET = os.getenv("ABAX_SIGN_SECRET", "dev-secret-change-in-prod")
SIGN_EXPIRY = 3600


def _read_file_sync(sandbox_id: str, path: str) -> str:
    container = get_container(sandbox_id)
    exit_code, output = container.exec_run(["cat", path])
    if exit_code != 0:
        raise FileNotFoundError(f"{path}: {output.decode('utf-8', errors='replace')}")
    return output.decode("utf-8", errors="replace")


async def read_file(sandbox_id: str, path: str) -> str:
    return await asyncio.to_thread(_read_file_sync, sandbox_id, path)


def _write_file_sync(sandbox_id: str, path: str, content: str) -> None:
    container = get_container(sandbox_id)
    data = content.encode("utf-8")
    tarstream = io.BytesIO()
    tarinfo = tarfile.TarInfo(name=os.path.basename(path))
    tarinfo.size = len(data)
    with tarfile.open(fileobj=tarstream, mode="w") as tar:
        tar.addfile(tarinfo, io.BytesIO(data))
    tarstream.seek(0)
    container.put_archive(os.path.dirname(path) or "/", tarstream)


async def write_file(sandbox_id: str, path: str, content: str) -> None:
    await asyncio.to_thread(_write_file_sync, sandbox_id, path, content)


def _read_file_bytes_sync(sandbox_id: str, path: str) -> tuple[bytes, str]:
    container = get_container(sandbox_id)
    bits, _ = container.get_archive(path)
    tarstream = io.BytesIO()
    for chunk in bits:
        tarstream.write(chunk)
    tarstream.seek(0)
    with tarfile.open(fileobj=tarstream) as tar:
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        return f.read(), member.name


async def read_file_bytes(sandbox_id: str, path: str) -> tuple[bytes, str]:
    return await asyncio.to_thread(_read_file_bytes_sync, sandbox_id, path)


def generate_download_token(sandbox_id: str, path: str) -> str:
    expires = int(time.time()) + SIGN_EXPIRY
    payload = f"{sandbox_id}:{path}:{expires}"
    sig = hmac.new(SIGN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{sandbox_id}:{path}:{expires}:{sig}"


def verify_download_token(token: str) -> tuple[str, str]:
    parts = token.split(":", 3)
    if len(parts) != 4:
        raise ValueError("invalid token format")

    sandbox_id, path, expires_str, sig = parts
    expires = int(expires_str)

    if time.time() > expires:
        raise ValueError("token expired")

    expected_payload = f"{sandbox_id}:{path}:{expires}"
    expected_sig = hmac.new(
        SIGN_SECRET.encode(), expected_payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("invalid signature")

    return sandbox_id, path
```

**Step 6: 更新 gateway/main.py 路由层**

所有路由调用现在需要 `await`（函数签名已经是 `async def`，只需把调用改为 await）：

```python
from fastapi import FastAPI, HTTPException, WebSocket, Response
from docker.errors import NotFound

from gateway.models import CreateSandboxRequest, ExecRequest, FileContent, SandboxInfo
from gateway import sandbox, executor, files

app = FastAPI(title="Abax Gateway", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Sandbox lifecycle ---


@app.post("/sandboxes", response_model=SandboxInfo)
async def create_sandbox(req: CreateSandboxRequest):
    return await sandbox.create_sandbox(req.user_id)


@app.get("/sandboxes", response_model=list[SandboxInfo])
async def list_sandboxes():
    return await sandbox.list_sandboxes()


@app.get("/sandboxes/{sandbox_id}", response_model=SandboxInfo)
async def get_sandbox(sandbox_id: str):
    try:
        return await sandbox.get_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.post("/sandboxes/{sandbox_id}/stop", response_model=SandboxInfo)
async def stop_sandbox(sandbox_id: str):
    try:
        return await sandbox.stop_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.delete("/sandboxes/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str):
    try:
        await sandbox.destroy_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


# --- Command execution ---


@app.post("/sandboxes/{sandbox_id}/exec")
async def exec_command(sandbox_id: str, req: ExecRequest):
    try:
        return await executor.exec_command(sandbox_id, req.command, req.timeout)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.websocket("/sandboxes/{sandbox_id}/stream")
async def stream_command(websocket: WebSocket, sandbox_id: str):
    await executor.stream_command(sandbox_id, websocket)


# --- File operations ---


@app.get("/sandboxes/{sandbox_id}/files/{path:path}")
async def read_file(sandbox_id: str, path: str):
    try:
        content = await files.read_file(sandbox_id, f"/{path}")
        return FileContent(content=content, path=f"/{path}")
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.put("/sandboxes/{sandbox_id}/files/{path:path}")
async def write_file(sandbox_id: str, path: str, req: FileContent):
    try:
        await files.write_file(sandbox_id, f"/{path}", req.content)
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.get("/sandboxes/{sandbox_id}/files-url/{path:path}")
async def get_download_url(sandbox_id: str, path: str):
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
```

**Step 7: 跑测试确认通过**

```bash
pytest tests/ -v
```

**Step 8: Commit**

```bash
git add gateway/sandbox.py gateway/executor.py gateway/files.py gateway/main.py tests/test_async.py
git commit -m "refactor: async gateway — move blocking Docker calls to thread pool"
```

---

## Task 3: 健康检查增强（可并行）

**Files:**
- Modify: `gateway/models.py`（添加 HealthResponse model）

注意：此 Task 只添加 model。实际的 `/health` 端点修改在 Task 4 中完成（依赖 Task 2 的异步化）。

**Step 1: 写测试**

创建 `tests/test_health.py`：

```python
"""Test enhanced health check."""
import pytest
from gateway.models import HealthResponse


def test_health_response_model():
    r = HealthResponse(
        status="ok",
        docker_connected=True,
        sandbox_image_ready=True,
        active_sandboxes=0,
    )
    assert r.status == "ok"
    assert r.docker_connected is True


def test_health_response_degraded():
    r = HealthResponse(
        status="degraded",
        docker_connected=True,
        sandbox_image_ready=False,
        active_sandboxes=0,
    )
    assert r.status == "degraded"
```

**Step 2: 跑测试确认失败**

```bash
pytest tests/test_health.py -v
```

**Step 3: 添加 HealthResponse model 到 gateway/models.py**

在现有 models 末尾追加：

```python
class HealthResponse(BaseModel):
    status: str  # "ok", "degraded", "error"
    docker_connected: bool
    sandbox_image_ready: bool
    active_sandboxes: int
```

**Step 4: 跑测试确认通过**

```bash
pytest tests/test_health.py -v
```

**Step 5: Commit**

```bash
git add gateway/models.py tests/test_health.py
git commit -m "feat: add HealthResponse model for enhanced health check"
```

---

## Task 4: 容器 GC + 健康检查端点（依赖 Task 2 + 3）

**Files:**
- Create: `gateway/gc.py`
- Modify: `gateway/main.py`（添加 lifespan + 更新 health 端点）
- Test: `tests/test_gc.py`

**Step 1: 写 GC 测试**

创建 `tests/test_gc.py`：

```python
"""Test container garbage collection."""
import pytest
from httpx import AsyncClient, ASGITransport
from gateway.main import app

transport = ASGITransport(app=app)


@pytest.mark.asyncio
async def test_gc_cleans_idle_containers():
    """Create a container, stop it, run GC, verify it's removed."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create and immediately stop
        r = await client.post("/sandboxes", json={"user_id": "test-gc"})
        sid = r.json()["sandbox_id"]
        await client.post(f"/sandboxes/{sid}/stop")

        # GC should clean exited containers
        from gateway.gc import collect_garbage
        removed = await collect_garbage(max_idle_seconds=0)
        assert sid in removed


@pytest.mark.asyncio
async def test_gc_preserves_running_containers():
    """Running containers should not be removed by GC."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/sandboxes", json={"user_id": "test-gc-keep"})
        sid = r.json()["sandbox_id"]

        try:
            from gateway.gc import collect_garbage
            removed = await collect_garbage(max_idle_seconds=0)
            assert sid not in removed
        finally:
            await client.delete(f"/sandboxes/{sid}")


@pytest.mark.asyncio
async def test_health_shows_docker_status():
    """Health endpoint should report Docker connectivity."""
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "docker_connected" in data
        assert "sandbox_image_ready" in data
        assert "active_sandboxes" in data
```

**Step 2: 跑测试确认失败**

```bash
pytest tests/test_gc.py -v
```

**Step 3: 实现 gateway/gc.py**

```python
import asyncio
import logging
import os

import docker

LABEL_PREFIX = "abax"
GC_INTERVAL = int(os.getenv("ABAX_GC_INTERVAL", "60"))  # seconds
MAX_IDLE_SECONDS = int(os.getenv("ABAX_MAX_IDLE", "1800"))  # 30 minutes

logger = logging.getLogger("abax.gc")
client = docker.from_env()


async def collect_garbage(max_idle_seconds: int = MAX_IDLE_SECONDS) -> list[str]:
    """Remove exited abax containers. Returns list of removed container IDs."""

    def _collect():
        removed = []
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.managed=true", "status": "exited"},
        )
        for container in containers:
            cid = container.id[:12]
            logger.info("GC removing exited container %s", cid)
            container.remove(force=True)
            removed.append(cid)
        return removed

    return await asyncio.to_thread(_collect)


async def gc_loop():
    """Background loop that runs GC periodically."""
    logger.info("GC loop started (interval=%ds, max_idle=%ds)", GC_INTERVAL, MAX_IDLE_SECONDS)
    while True:
        await asyncio.sleep(GC_INTERVAL)
        try:
            removed = await collect_garbage()
            if removed:
                logger.info("GC removed %d containers: %s", len(removed), removed)
        except Exception:
            logger.exception("GC error")
```

**Step 4: 更新 gateway/main.py — 添加 lifespan + 增强 health**

在 `gateway/main.py` 顶部添加 lifespan，替换 health 端点：

```python
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, Response
from docker.errors import NotFound

from gateway.models import (
    CreateSandboxRequest, ExecRequest, FileContent,
    HealthResponse, SandboxInfo,
)
from gateway import sandbox, executor, files
from gateway.gc import gc_loop, collect_garbage

logger = logging.getLogger("abax.gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: clean orphan containers from previous runs
    removed = await collect_garbage(max_idle_seconds=0)
    if removed:
        logger.info("Startup cleanup: removed %d orphan containers", len(removed))

    # Start background GC
    gc_task = asyncio.create_task(gc_loop())
    yield
    # Shutdown
    gc_task.cancel()


app = FastAPI(title="Abax Gateway", version="0.1.0", lifespan=lifespan)


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

    status = "ok" if (docker_ok and image_ok) else "degraded" if docker_ok else "error"
    return HealthResponse(
        status=status,
        docker_connected=docker_ok,
        sandbox_image_ready=image_ok,
        active_sandboxes=active,
    )


# ... rest of routes unchanged (from Task 2) ...
```

**Step 5: 跑全量测试**

```bash
pytest tests/ -v
```

**Step 6: Commit**

```bash
git add gateway/gc.py gateway/main.py tests/test_gc.py
git commit -m "feat: container GC with background loop + enhanced health check"
```

---

## Task 5: 测试自动化 + 全量验证（依赖 Task 1-4）

**Files:**
- Modify: `tests/conftest.py`（新建，集中 fixture + 镜像预检）
- Modify: `tests/test_gateway.py`（更新为使用共享 fixture）
- Delete: `tests/__init__.py` 内容不变

**Step 1: 创建 tests/conftest.py**

```python
"""Shared fixtures and pre-flight checks for all tests."""
import subprocess

import docker
import pytest
from httpx import AsyncClient, ASGITransport
from gateway.main import app

SANDBOX_IMAGE = "abax-sandbox"


def pytest_configure(config):
    """Pre-flight: verify Docker is running and sandbox image exists."""
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.exit("Docker daemon is not running. Start Docker first.", returncode=1)

    try:
        c.images.get(SANDBOX_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"Image '{SANDBOX_IMAGE}' not found, building...")
        result = subprocess.run(
            ["docker", "build", "-t", SANDBOX_IMAGE, "./sandbox-image"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.exit(f"Failed to build sandbox image:\n{result.stderr}", returncode=1)
        print("Image built successfully.")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def sandbox_id(client):
    """Create a sandbox, yield its ID, cleanup after."""
    r = await client.post("/sandboxes", json={"user_id": "test-user"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    yield sid
    # Cleanup — ignore errors if already removed
    await client.delete(f"/sandboxes/{sid}")
```

**Step 2: 简化 tests/test_gateway.py，使用共享 fixture**

```python
"""Integration tests for Abax Gateway."""
import pytest


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["docker_connected"] is True
    assert data["sandbox_image_ready"] is True


@pytest.mark.asyncio
async def test_create_and_list_sandbox(client):
    r = await client.post("/sandboxes", json={"user_id": "test-list"})
    assert r.status_code == 200
    info = r.json()
    assert info["user_id"] == "test-list"
    assert info["status"] == "running"

    r = await client.get("/sandboxes")
    ids = [s["sandbox_id"] for s in r.json()]
    assert info["sandbox_id"] in ids

    await client.delete(f"/sandboxes/{info['sandbox_id']}")


@pytest.mark.asyncio
async def test_exec_command(client, sandbox_id):
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": "echo hello"},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"


@pytest.mark.asyncio
async def test_exec_python(client, sandbox_id):
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": "python3 -c 'print(1+1)'"},
    )
    result = r.json()
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "2"


@pytest.mark.asyncio
async def test_exec_beancount(client, sandbox_id):
    ledger = 'option "title" "Test"\n2026-01-01 open Assets:Cash CNY\n'
    await client.put(
        f"/sandboxes/{sandbox_id}/files/data/test.beancount",
        json={"content": ledger, "path": "/data/test.beancount"},
    )
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": "bean-check /data/test.beancount"},
    )
    assert r.json()["exit_code"] == 0


@pytest.mark.asyncio
async def test_file_read_write(client, sandbox_id):
    content = "hello from abax"
    await client.put(
        f"/sandboxes/{sandbox_id}/files/data/test.txt",
        json={"content": content, "path": "/data/test.txt"},
    )
    r = await client.get(f"/sandboxes/{sandbox_id}/files/data/test.txt")
    assert r.status_code == 200
    assert r.json()["content"] == content


@pytest.mark.asyncio
async def test_file_not_found(client, sandbox_id):
    r = await client.get(f"/sandboxes/{sandbox_id}/files/data/nonexistent.txt")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_sandbox_not_found(client):
    r = await client.get("/sandboxes/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_stop_and_destroy(client):
    r = await client.post("/sandboxes", json={"user_id": "test-stop"})
    info = r.json()
    sid = info["sandbox_id"]

    r = await client.post(f"/sandboxes/{sid}/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "exited"

    r = await client.delete(f"/sandboxes/{sid}")
    assert r.status_code == 204
```

**Step 3: 全量测试**

```bash
make test
```

预期：所有测试通过，无孤儿容器。

**Step 4: 验证 make clean 有效**

```bash
make clean
docker ps -a --filter "label=abax.managed=true"
```

预期：无输出（所有 abax 容器已清理）。

**Step 5: Commit**

```bash
git add tests/conftest.py tests/test_gateway.py tests/test_async.py tests/test_health.py tests/test_gc.py
git commit -m "test: automated pre-flight checks, shared fixtures, full coverage"
```

---

## 总结

```
         ┌──────────────────────────────────────┐
并行阶段  │ Task 1         Task 2       Task 3   │
         │ compose+make   async化      model     │
         └──────┬──────────┬────────────┬────────┘
                │          │            │
                └──────────┴─────┬──────┘
                                 ▼
         ┌──────────────────────────────────────┐
串行阶段  │ Task 4: GC + health endpoint         │
         └──────────────────┬───────────────────┘
                            ▼
         ┌──────────────────────────────────────┐
         │ Task 5: 测试自动化 + 全量验证          │
         └──────────────────────────────────────┘
```

完成后的开发体验：
- `make dev` — 一键 docker-compose 启动
- `make test` — 自动构建镜像 + 跑全量测试
- `make clean` — 清理所有容器
- Gateway 不阻塞，后台自动 GC，health 端点报告真实状态
