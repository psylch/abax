from pydantic import BaseModel, Field


class CreateSandboxRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")


class SandboxInfo(BaseModel):
    sandbox_id: str
    user_id: str
    status: str  # "running", "paused", "exited", "created"


class ExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=65536)
    timeout: int = Field(default=30, ge=1, le=300)


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class FileContent(BaseModel):
    content: str = Field(max_length=10 * 1024 * 1024)  # 10MB
    path: str


class HealthResponse(BaseModel):
    status: str  # "ok", "degraded", "error"
    docker_connected: bool
    sandbox_image_ready: bool
    active_sandboxes: int
    warm_pool_size: int = 0


class DirEntry(BaseModel):
    name: str
    is_dir: bool
    size: int  # bytes, -1 for dirs


class DirListing(BaseModel):
    path: str
    entries: list[DirEntry]


class BinaryFileContent(BaseModel):
    data_b64: str = Field(max_length=15 * 1024 * 1024)  # ~10MB decoded
    path: str


# --- Browser request models ---


class BrowserNavigateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class BrowserScreenshotRequest(BaseModel):
    full_page: bool = False


class BrowserClickRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1024)


class BrowserTypeRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1024)
    text: str = Field(max_length=65536)
