from fastapi import FastAPI, HTTPException, WebSocket, Response
from docker.errors import NotFound

from gateway.models import CreateSandboxRequest, ExecRequest, FileContent, SandboxInfo
from gateway import sandbox, executor, files

app = FastAPI(title="Abax Gateway", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Sandbox lifecycle ---


@app.post("/sandboxes", response_model=SandboxInfo)
async def create_sandbox(req: CreateSandboxRequest):
    return sandbox.create_sandbox(req.user_id)


@app.get("/sandboxes", response_model=list[SandboxInfo])
async def list_sandboxes():
    return sandbox.list_sandboxes()


@app.get("/sandboxes/{sandbox_id}", response_model=SandboxInfo)
async def get_sandbox(sandbox_id: str):
    try:
        return sandbox.get_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.post("/sandboxes/{sandbox_id}/stop", response_model=SandboxInfo)
async def stop_sandbox(sandbox_id: str):
    try:
        return sandbox.stop_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.delete("/sandboxes/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str):
    try:
        sandbox.destroy_sandbox(sandbox_id)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


# --- Command execution ---


@app.post("/sandboxes/{sandbox_id}/exec")
async def exec_command(sandbox_id: str, req: ExecRequest):
    try:
        return executor.exec_command(sandbox_id, req.command, req.timeout)
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.websocket("/sandboxes/{sandbox_id}/stream")
async def stream_command(websocket: WebSocket, sandbox_id: str):
    await executor.stream_command(sandbox_id, websocket)


# --- File operations ---


@app.get("/sandboxes/{sandbox_id}/files/{path:path}")
async def read_file(sandbox_id: str, path: str):
    try:
        content = files.read_file(sandbox_id, f"/{path}")
        return FileContent(content=content, path=f"/{path}")
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.put("/sandboxes/{sandbox_id}/files/{path:path}")
async def write_file(sandbox_id: str, path: str, req: FileContent):
    try:
        files.write_file(sandbox_id, f"/{path}", req.content)
        return {"ok": True}
    except NotFound:
        raise HTTPException(404, "sandbox not found")


@app.get("/sandboxes/{sandbox_id}/files-url/{path:path}")
async def get_download_url(sandbox_id: str, path: str):
    """Generate a signed download URL for a file."""
    token = files.generate_download_token(sandbox_id, f"/{path}")
    return {"url": f"/files/{token}"}


@app.get("/files/{token}")
async def download_file(token: str):
    try:
        sid, path = files.verify_download_token(token)
        data, filename = files.read_file_bytes(sid, path)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as e:
        raise HTTPException(403, str(e))
    except NotFound:
        raise HTTPException(404, "sandbox not found")
