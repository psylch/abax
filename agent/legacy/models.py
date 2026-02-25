"""Session, Chat, and LLM proxy Pydantic models for the agent layer.

Extracted from gateway/models.py during the Infra layer separation refactor.
Only includes models relevant to agent orchestration — sandbox/exec/file
models stay in gateway/models.py.
"""

from pydantic import BaseModel, Field


# --- Session / message models ---


class CreateSessionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    title: str | None = None


class SessionInfo(BaseModel):
    session_id: str
    user_id: str
    title: str | None = None
    sandbox_id: str | None = None
    created_at: float
    last_active_at: float


class MessageRequest(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=10 * 1024 * 1024)
    tool_calls: str | None = None
    tool_results: str | None = None


class MessageInfo(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    tool_calls: str | None = None
    tool_results: str | None = None
    created_at: float


class SessionHistoryResponse(BaseModel):
    session_id: str
    messages: list[MessageInfo]


# --- Chat / Agent models ---


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10 * 1024 * 1024)


class ChatResponse(BaseModel):
    response: str
    tier: str  # "tier1", "tier2", "tier3"
    sandbox_id: str | None = None
    tool_calls_count: int = 0


# --- LLM proxy model ---


class LLMProxyRequest(BaseModel):
    model: str
    max_tokens: int = 4096
    system: str | None = None
    messages: list[dict]
    tools: list[dict] | None = None
    stream: bool = False
