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
