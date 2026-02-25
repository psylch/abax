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
