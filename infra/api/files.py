"""File operation routes."""
import base64

from fastapi import APIRouter, Depends, HTTPException, Response
from docker.errors import NotFound

from infra.auth import verify_api_key
from infra.core import files
from infra.core.store import store
from infra.events import publish as emit_event
from infra.models import (
    BinaryFileContent, DirEntry, DirListing, FileBatchRequest, FileContent,
)

router = APIRouter(tags=["files"])
Auth = Depends(verify_api_key)


@router.get("/sandboxes/{sandbox_id}/files/{path:path}")
async def read_file(sandbox_id: str, path: str, _=Auth):
    try:
        store.record_activity(sandbox_id)
        content = await files.read_file(sandbox_id, f"/{path}")
        return FileContent(content=content, path=f"/{path}")
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.put("/sandboxes/{sandbox_id}/files/{path:path}")
async def write_file(sandbox_id: str, path: str, req: FileContent, _=Auth):
    try:
        store.record_activity(sandbox_id)
        await files.write_file(sandbox_id, f"/{path}", req.content)
        await emit_event(sandbox_id, "file.written", {"path": f"/{path}"})
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@router.get("/sandboxes/{sandbox_id}/files-url/{path:path}")
async def get_download_url(sandbox_id: str, path: str, _=Auth):
    store.record_activity(sandbox_id)
    token = files.generate_download_token(sandbox_id, f"/{path}")
    return {"url": f"/files/{token}"}


@router.get("/files/{token}")
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


@router.get("/sandboxes/{sandbox_id}/ls/{path:path}", response_model=DirListing)
async def list_dir(sandbox_id: str, path: str, _=Auth):
    try:
        store.record_activity(sandbox_id)
        entries_raw = await files.list_dir(sandbox_id, f"/{path}")
        entries = [DirEntry(**e) for e in entries_raw]
        return DirListing(path=f"/{path}", entries=entries)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.post("/sandboxes/{sandbox_id}/files-batch")
async def batch_file_ops(sandbox_id: str, req: FileBatchRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        ops = [op.model_dump() for op in req.operations]
        results = await files.batch_file_ops(sandbox_id, ops)
        return {"results": results}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@router.put("/sandboxes/{sandbox_id}/files-bin/{path:path}")
async def write_file_binary(sandbox_id: str, path: str, req: BinaryFileContent, _=Auth):
    try:
        store.record_activity(sandbox_id)
        try:
            data = base64.b64decode(req.data_b64)
        except Exception:
            raise HTTPException(400, "invalid base64 data")
        await files.write_file_bytes(sandbox_id, f"/{path}", data)
        await emit_event(sandbox_id, "file.written", {"path": f"/{path}"})
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")
