"""Universal sandbox daemon running inside each container.

Provides HTTP endpoints for:
- File operations (read/write/list/batch)
- Command execution (sync + streaming via WebSocket)
- Browser automation (Playwright)
- Health checks

Runs on 0.0.0.0:8331 and is started as the container entrypoint.
"""

import asyncio
import base64
import json
import shlex
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Browser state (lazy-init -- only started when first browser endpoint is hit)
# ---------------------------------------------------------------------------

_playwright = None
_browser = None
_page = None
_browser_lock = asyncio.Lock()


async def _ensure_browser():
    """Lazily start the Playwright browser on first use."""
    global _playwright, _browser, _page
    if _page is not None:
        return
    async with _browser_lock:
        if _page is not None:
            return
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        _page = await _browser.new_page()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global _browser, _playwright
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()


app = FastAPI(title="Sandbox Daemon", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30


class FileWriteRequest(BaseModel):
    content: str


class BatchOp(BaseModel):
    op: str  # "read", "write", "list"
    path: str
    content: str | None = None


class BatchRequest(BaseModel):
    operations: list[BatchOp]


# ---------------------------------------------------------------------------
# Shared exec helper
# ---------------------------------------------------------------------------


async def _run_command(command: str, timeout: int = 30) -> dict:
    """Run a shell command with dual timeout (Linux timeout + asyncio fallback).

    Returns dict with stdout, stderr, exit_code.
    """
    if shutil.which("timeout"):
        wrapped = f"timeout {timeout} bash -c {shlex.quote(command)}"
        grace = timeout + 5
    else:
        wrapped = f"bash -c {shlex.quote(command)}"
        grace = timeout

    proc = await asyncio.create_subprocess_shell(
        wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=grace
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=2
            )
        except asyncio.TimeoutError:
            stdout_bytes, stderr_bytes = b"", b""
        return {
            "stdout": stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            "stderr": "command timed out",
            "exit_code": 124,
        }

    return {
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "exit_code": proc.returncode,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "has_browser": _page is not None}


# ---------------------------------------------------------------------------
# Exec endpoints
# ---------------------------------------------------------------------------


@app.post("/exec")
async def exec_command(req: ExecRequest):
    try:
        return await _run_command(req.command, req.timeout)
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


@app.websocket("/exec/stream")
async def exec_stream(ws: WebSocket):
    await ws.accept()
    try:
        msg = await ws.receive_text()
        data = json.loads(msg)
        command = data.get("command", "")
        if not command:
            await ws.send_json({"type": "error", "data": "empty command"})
            await ws.close()
            return

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            await ws.send_json(
                {"type": "stdout", "data": chunk.decode("utf-8", errors="replace")}
            )

        await proc.wait()
        await ws.send_json({"type": "exit", "data": str(proc.returncode)})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": str(e)})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# File endpoints
# ---------------------------------------------------------------------------


@app.get("/files/{path:path}")
async def read_file(path: str):
    full_path = f"/{path}"
    try:
        content = Path(full_path).read_text(encoding="utf-8", errors="replace")
        return {"content": content, "path": full_path}
    except FileNotFoundError:
        return {"error": f"not found: {full_path}", "status": 404}
    except IsADirectoryError:
        return {"error": f"is a directory: {full_path}", "status": 400}
    except PermissionError:
        return {"error": f"permission denied: {full_path}", "status": 403}


@app.put("/files/{path:path}")
async def write_file(path: str, req: FileWriteRequest):
    full_path = f"/{path}"
    try:
        p = Path(full_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(req.content, encoding="utf-8")
        return {"ok": True, "path": full_path}
    except PermissionError:
        return {"error": f"permission denied: {full_path}", "status": 403}


def _list_dir_entries(dir_path: Path) -> list[dict]:
    """List directory entries as dicts with name, is_dir, size."""
    entries = []
    for entry in sorted(dir_path.iterdir(), key=lambda e: e.name):
        is_dir = entry.is_dir()
        try:
            size = -1 if is_dir else entry.stat().st_size
        except OSError:
            size = -1
        entries.append({"name": entry.name, "is_dir": is_dir, "size": size})
    return entries


@app.get("/ls/{path:path}")
async def list_dir(path: str):
    full_path = f"/{path}"
    try:
        p = Path(full_path)
        if not p.is_dir():
            return {"error": f"not a directory: {full_path}", "status": 400}
        return {"path": full_path, "entries": _list_dir_entries(p)}
    except FileNotFoundError:
        return {"error": f"not found: {full_path}", "status": 404}
    except PermissionError:
        return {"error": f"permission denied: {full_path}", "status": 403}


@app.post("/files/batch")
async def batch_file_ops(req: BatchRequest):
    results = []
    for op in req.operations:
        full_path = op.path if op.path.startswith("/") else f"/{op.path}"
        try:
            if op.op == "read":
                content = Path(full_path).read_text(encoding="utf-8", errors="replace")
                results.append({"ok": True, "content": content, "path": full_path})
            elif op.op == "write":
                p = Path(full_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(op.content or "", encoding="utf-8")
                results.append({"ok": True, "path": full_path})
            elif op.op == "list":
                results.append({
                    "ok": True,
                    "path": full_path,
                    "entries": _list_dir_entries(Path(full_path)),
                })
            else:
                results.append({"ok": False, "error": f"unknown op: {op.op}"})
        except Exception as e:
            results.append({"ok": False, "error": str(e), "path": full_path})
    return {"results": results}


# ---------------------------------------------------------------------------
# Browser endpoints (same API as old browser_server.py)
# ---------------------------------------------------------------------------


@app.post("/navigate")
async def navigate(req: dict):
    await _ensure_browser()
    url = req["url"]
    await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
    return {"title": await _page.title(), "url": _page.url}


@app.post("/screenshot")
async def screenshot(req: dict = {}):
    await _ensure_browser()
    data = await _page.screenshot(full_page=req.get("full_page", False))
    return {"data_b64": base64.b64encode(data).decode(), "format": "png"}


@app.post("/click")
async def click(req: dict):
    await _ensure_browser()
    await _page.click(req["selector"], timeout=10000)
    return {"ok": True}


@app.post("/type")
async def type_text(req: dict):
    await _ensure_browser()
    await _page.fill(req["selector"], req["text"], timeout=10000)
    return {"ok": True}


@app.get("/content")
async def get_content(mode: str = "text"):
    await _ensure_browser()
    if mode == "html":
        content = await _page.content()
    else:
        content = await _page.inner_text("body")
    return {"content": content, "url": _page.url, "title": await _page.title()}


import uuid as _uuid

# ---------------------------------------------------------------------------
# Persistent bash sessions
# ---------------------------------------------------------------------------

_bash_sessions: dict[str, asyncio.subprocess.Process] = {}


class BashRunRequest(BaseModel):
    command: str
    timeout: int = 30


@app.post("/bash/create")
async def create_bash():
    """Create a persistent bash process."""
    bash_id = _uuid.uuid4().hex[:12]
    proc = await asyncio.create_subprocess_exec(
        "bash", "--norc", "--noprofile",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
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
    exit_code = 0
    try:
        async with asyncio.timeout(req.timeout):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    exit_code = -1
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                if decoded.startswith(delimiter):
                    parts = decoded.split()
                    exit_code = int(parts[1]) if len(parts) > 1 else 0
                    break
                output_lines.append(decoded)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8331)
