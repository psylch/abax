from pydantic import BaseModel


class CreateSandboxRequest(BaseModel):
    user_id: str


class SandboxInfo(BaseModel):
    sandbox_id: str
    user_id: str
    status: str  # "running", "exited", "created"


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30  # seconds


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class FileContent(BaseModel):
    content: str
    path: str
