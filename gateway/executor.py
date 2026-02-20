import asyncio
import json
import shlex
import time

from fastapi import WebSocket, WebSocketDisconnect

from gateway.models import ExecResult
from gateway.sandbox import get_container


def _exec_sync(sandbox_id: str, command: str, timeout: int = 30) -> ExecResult:
    container = get_container(sandbox_id)
    wrapped = f"timeout {timeout} bash -c {shlex.quote(command)}"
    start = time.monotonic()
    exit_code, output = container.exec_run(
        ["bash", "-c", wrapped],
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
    return await asyncio.wait_for(
        asyncio.to_thread(_exec_sync, sandbox_id, command, timeout),
        timeout=timeout + 5,  # async fallback: 5s grace over container-level timeout
    )


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
