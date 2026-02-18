import json
import time

from fastapi import WebSocket, WebSocketDisconnect

from gateway.models import ExecResult
from gateway.sandbox import get_container


def exec_command(sandbox_id: str, command: str, timeout: int = 30) -> ExecResult:
    container = get_container(sandbox_id)
    start = time.monotonic()
    exit_code, output = container.exec_run(
        ["bash", "-c", command],
        demux=True,  # separate stdout/stderr
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


async def stream_command(sandbox_id: str, websocket: WebSocket):
    await websocket.accept()

    try:
        # Wait for the command from the client
        msg = await websocket.receive_text()
        data = json.loads(msg)
        command = data.get("command", "")

        if not command:
            await websocket.send_json({"type": "error", "data": "empty command"})
            await websocket.close()
            return

        container = get_container(sandbox_id)

        # Create exec instance with streaming
        exec_instance = container.client.api.exec_create(
            container.id,
            ["bash", "-c", command],
            stdout=True,
            stderr=True,
        )

        output = container.client.api.exec_start(exec_instance["Id"], stream=True)

        for chunk in output:
            text = chunk.decode("utf-8", errors="replace")
            await websocket.send_json({"type": "stdout", "data": text})

        # Get exit code
        inspect = container.client.api.exec_inspect(exec_instance["Id"])
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
