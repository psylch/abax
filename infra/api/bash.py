"""Persistent bash session routes."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from docker.errors import NotFound
from pydantic import BaseModel, Field

from infra.auth import verify_api_key
from infra.core.daemon import request_sync
from infra.core.store import store

router = APIRouter(prefix="/sandboxes", tags=["bash"])
Auth = Depends(verify_api_key)


class BashRunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=65536)
    timeout: int = Field(default=30, ge=1, le=300)


@router.post("/{sandbox_id}/bash")
async def create_bash(sandbox_id: str, _=Auth):
    """Create a persistent bash session in the sandbox."""
    try:
        store.record_activity(sandbox_id)
        result = await asyncio.to_thread(
            request_sync, sandbox_id, "POST", "/bash/create", {}
        )
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/{sandbox_id}/bash/{bash_id}/run")
async def bash_run(sandbox_id: str, bash_id: str, req: BashRunRequest, _=Auth):
    """Run a command in an existing bash session."""
    try:
        store.record_activity(sandbox_id)
        result = await asyncio.to_thread(
            request_sync, sandbox_id, "POST", f"/bash/{bash_id}/run",
            {"command": req.command, "timeout": req.timeout},
            timeout=req.timeout + 10,
        )
        if result.get("status") == 404:
            raise HTTPException(404, result.get("error", "bash session not found"))
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.delete("/{sandbox_id}/bash/{bash_id}")
async def delete_bash(sandbox_id: str, bash_id: str, _=Auth):
    """Close a persistent bash session."""
    try:
        result = await asyncio.to_thread(
            request_sync, sandbox_id, "DELETE", f"/bash/{bash_id}"
        )
        if result.get("status") == 404:
            raise HTTPException(404, result.get("error", "bash session not found"))
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))
