"""Interactive PTY terminal for sandbox containers via WebSocket."""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect
from docker.errors import NotFound

from gateway.sandbox import get_container

logger = logging.getLogger("abax.terminal")


async def handle_terminal(websocket: WebSocket, sandbox_id: str):
    """Handle an interactive PTY terminal session over WebSocket."""
    await websocket.accept()

    try:
        container = get_container(sandbox_id)
    except NotFound:
        await websocket.send_json({"type": "error", "message": "sandbox not found"})
        await websocket.close()
        return

    # Create exec instance with PTY
    def _create_exec():
        exec_instance = container.client.api.exec_create(
            container.id,
            "bash",
            stdin=True,
            tty=True,
            stdout=True,
            stderr=True,
        )
        socket = container.client.api.exec_start(
            exec_instance["Id"],
            socket=True,
            tty=True,
        )
        return exec_instance, socket

    try:
        exec_instance, sock = await asyncio.to_thread(_create_exec)
    except Exception as e:
        logger.error("Failed to create PTY exec for sandbox %s: %s", sandbox_id, e)
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.close()
        return

    # Access the underlying socket for bidirectional communication
    raw_sock = sock._sock

    # Reader task: read from Docker PTY socket -> send to WebSocket
    async def reader():
        try:
            while True:
                data = await asyncio.to_thread(raw_sock.recv, 4096)
                if not data:
                    break
                await websocket.send_json({
                    "type": "stdout",
                    "data": data.decode("utf-8", errors="replace"),
                })
        except (OSError, WebSocketDisconnect):
            pass
        except Exception:
            logger.debug("Terminal reader error for %s", sandbox_id, exc_info=True)

    # Writer task: read from WebSocket → send to Docker PTY socket
    async def writer():
        try:
            while True:
                msg = await websocket.receive_json()
                msg_type = msg.get("type", "")

                if msg_type == "stdin":
                    data = msg.get("data", "")
                    await asyncio.to_thread(raw_sock.sendall, data.encode("utf-8"))

                elif msg_type == "resize":
                    cols = msg.get("cols", 80)
                    rows = msg.get("rows", 24)
                    await asyncio.to_thread(
                        container.client.api.exec_resize,
                        exec_instance["Id"],
                        height=rows,
                        width=cols,
                    )
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("Terminal writer error for %s", sandbox_id, exc_info=True)

    # Run reader and writer concurrently
    reader_task = asyncio.create_task(reader())
    writer_task = asyncio.create_task(writer())

    try:
        done, pending = await asyncio.wait(
            [reader_task, writer_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
