"""Pydantic models for agent API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=100_000)
    user_id: str = Field(default="anonymous", min_length=1, max_length=64)


class ChatResponse(BaseModel):
    session_id: str
    text: str
    tool_calls: list[dict]
    sandbox_id: str | None
    cost_usd: float | None


class SessionInfo(BaseModel):
    session_id: str
    user_id: str
    title: str | None
    sandbox_id: str | None
    created_at: float
    last_active_at: float


class MessageInfo(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    tool_calls: list[dict] | None
    created_at: float
