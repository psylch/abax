"""Universal sandbox daemon running inside each container.

Provides HTTP endpoints for:
- File operations (read/write/list/batch)
- Command execution (sync + streaming via WebSocket)
- Browser automation (Playwright)
- Health checks
- Agent turn (ReAct loop)

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


# ---------------------------------------------------------------------------
# Agent turn endpoint -- ReAct loop running locally inside the container
# ---------------------------------------------------------------------------


class AgentTurnRequest(BaseModel):
    messages: list[dict]
    system_prompt: str
    tools: list[dict]
    first_response: dict  # The initial LLM response (may contain tool_use)
    model: str = "claude-sonnet-4-20250514"
    max_turns: int = 20
    gateway_url: str = "http://host.docker.internal:8000"


def _format_exec_result(result: dict) -> str:
    """Format an exec result into a readable string."""
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"])
    if result.get("stderr"):
        parts.append(f"[stderr] {result['stderr']}")
    if result.get("exit_code", 0) != 0:
        parts.append(f"[exit code: {result['exit_code']}]")
    return "\n".join(parts) if parts else "(no output)"


async def _local_tool_exec(name: str, params: dict) -> str:
    """Execute a tool locally inside the container."""
    if name == "execute_command":
        command = params.get("command", "")
        timeout = params.get("timeout", 30)
        try:
            result = await _run_command(command, timeout)
            return _format_exec_result(result)
        except Exception as e:
            return f"Error: {e}"

    elif name == "write_file":
        path = params.get("path", "")
        content = params.get("content", "")
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} characters to {path}"
        except Exception as e:
            return f"Error: {e}"

    elif name == "read_file":
        path = params.get("path", "")
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error: {e}"

    elif name == "list_files":
        path = params.get("path", "/workspace")
        try:
            p = Path(path)
            if not p.is_dir():
                return f"Error: not a directory: {path}"
            lines = []
            for entry in sorted(p.iterdir(), key=lambda e: e.name):
                kind = "dir" if entry.is_dir() else "file"
                try:
                    size = "" if entry.is_dir() else str(entry.stat().st_size)
                except OSError:
                    size = ""
                lines.append(f"  {kind}  {entry.name}  {size}")
            return "\n".join(lines) if lines else "(empty directory)"
        except Exception as e:
            return f"Error: {e}"

    elif name == "browser_navigate":
        try:
            await _ensure_browser()
            url = params.get("url", "")
            await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return json.dumps({"title": await _page.title(), "url": _page.url})
        except Exception as e:
            return f"Error: {e}"

    elif name == "browser_screenshot":
        try:
            await _ensure_browser()
            data = await _page.screenshot(full_page=False)
            return f"Screenshot taken (png, {len(data)} bytes)"
        except Exception as e:
            return f"Error: {e}"

    elif name == "browser_content":
        try:
            await _ensure_browser()
            mode = params.get("mode", "text")
            if mode == "html":
                content = await _page.content()
            else:
                content = await _page.inner_text("body")
            if len(content) > 10000:
                content = content[:10000] + "\n... (truncated)"
            return content
        except Exception as e:
            return f"Error: {e}"

    else:
        return f"Unknown tool: {name}"


async def _call_llm(gateway_url: str, body: dict) -> dict:
    """Call LLM via the gateway's /llm/proxy endpoint."""
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{gateway_url}/llm/proxy",
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


@app.post("/agent/turn")
async def agent_turn(req: AgentTurnRequest):
    """Run a ReAct agent loop locally inside the container.

    Uses first_response from Gateway's initial LLM call, then continues
    tool execution locally until the LLM returns pure text.
    """
    messages = list(req.messages)
    turns = []
    tool_calls_count = 0
    response = req.first_response

    for _ in range(req.max_turns):
        # Extract content blocks
        assistant_content = response.get("content", [])
        tool_use_blocks = [b for b in assistant_content if b.get("type") == "tool_use"]
        text_blocks = [b for b in assistant_content if b.get("type") == "text"]

        # Record assistant turn
        text = "\n".join(b["text"] for b in text_blocks)
        turn_record = {"role": "assistant", "text": text}
        if tool_use_blocks:
            turn_record["tool_calls"] = tool_use_blocks
        turns.append(turn_record)

        # Add assistant message to conversation
        messages.append({"role": "assistant", "content": assistant_content})

        # If no tool use, we're done
        stop_reason = response.get("stop_reason", "end_turn")
        if stop_reason == "end_turn" or not tool_use_blocks:
            break

        # Execute tools locally
        tool_results = []
        for block in tool_use_blocks:
            tool_calls_count += 1
            result_text = await _local_tool_exec(block["name"], block.get("input", {}))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": result_text,
            })

        turns.append({"role": "tool_results", "results": tool_results})
        messages.append({"role": "user", "content": tool_results})

        # Call LLM again via gateway proxy
        try:
            response = await _call_llm(req.gateway_url, {
                "model": req.model,
                "max_tokens": 4096,
                "system": req.system_prompt,
                "messages": messages,
                "tools": req.tools,
            })
        except Exception as e:
            return {
                "response": f"(LLM call failed: {e})",
                "turns": turns,
                "tool_calls_count": tool_calls_count,
            }

    # Extract final text
    final_texts = [
        b["text"] for b in response.get("content", []) if b.get("type") == "text"
    ]
    final_response = "\n".join(final_texts) if final_texts else "(no response)"

    return {
        "response": final_response,
        "turns": turns,
        "tool_calls_count": tool_calls_count,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8331)
