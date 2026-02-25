import asyncio
import json
import time

from fastapi import WebSocket, WebSocketDisconnect

from infra.core.daemon import request_sync
from infra.models import ExecResult
from infra.core.sandbox import get_container


async def exec_command(sandbox_id: str, command: str, timeout: int = 30) -> ExecResult:
    """Execute a command via the in-container daemon."""
    start = time.monotonic()
    data = await asyncio.wait_for(
        asyncio.to_thread(
            request_sync,
            sandbox_id,
            "POST",
            "/exec",
            {"command": command, "timeout": timeout},
            timeout + 5,  # curl max-time: command timeout + 5s grace
        ),
        timeout=timeout + 10,  # async fallback
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    return ExecResult(
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", ""),
        exit_code=data.get("exit_code", -1),
        duration_ms=duration_ms,
    )


async def stream_command(sandbox_id: str, websocket: WebSocket):
    """Stream command output via WebSocket.

    For streaming, we still use docker exec directly since WebSocket proxying
    through curl is impractical. The daemon handles non-streaming exec.
    """
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

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _stream():
            output = container.client.api.exec_start(exec_instance["Id"], stream=True)
            for chunk in output:
                text = chunk.decode("utf-8", errors="replace")
                queue.put_nowait(text)
            queue.put_nowait(None)

        stream_task = asyncio.get_event_loop().run_in_executor(None, _stream)

        while True:
            text = await queue.get()
            if text is None:
                break
            await websocket.send_json({"type": "stdout", "data": text})

        await stream_task

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
