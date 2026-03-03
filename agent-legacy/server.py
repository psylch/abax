"""
Abax Agent Server — HTTP API for the frontend.

Sits between React frontend and the agent loop.
Frontend → Agent Server (:8001) → Gateway (:8000) → Docker container.

Usage:
  uvicorn agent.server:app --port 8001
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import anthropic
import httpx

from agent.loop import run_turn, run_turn_stream
from agent.session import Session
from agent.tools import ToolContext


def load_dotenv():
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

app = FastAPI(title="Abax Agent Server", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared state
GATEWAY_URL = os.getenv("ABAX_GATEWAY_URL", "http://localhost:8000")
_claude_client = None
_sandbox_id = None
_sessions: dict[str, Session] = {}


def get_claude_client() -> anthropic.Anthropic:
    global _claude_client
    if _claude_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        if base_url:
            _claude_client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        else:
            _claude_client = anthropic.Anthropic(api_key=api_key)
    return _claude_client


async def get_sandbox_id() -> str:
    global _sandbox_id
    if _sandbox_id is not None:
        return _sandbox_id

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
        r = await client.get("/sandboxes")
        sandboxes = [s for s in r.json() if s["status"] == "running"]
        if sandboxes:
            _sandbox_id = sandboxes[0]["sandbox_id"]
        else:
            r = await client.post("/sandboxes", json={"user_id": "web"})
            _sandbox_id = r.json()["sandbox_id"]
    return _sandbox_id


def get_session(session_id: str) -> Session:
    if session_id not in _sessions:
        path = Session(session_id).path
        if path.exists():
            _sessions[session_id] = Session.load(session_id)
        else:
            _sessions[session_id] = Session(session_id)
    return _sessions[session_id]


# --- API Models ---

class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    session_id: str
    blocks: list[dict]


class SessionInfo(BaseModel):
    id: str
    preview: str
    message_count: int


# --- Routes ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Non-streaming chat endpoint."""
    session = get_session(req.session_id)
    session.add_message("user", req.message)

    sandbox_id = await get_sandbox_id()
    ctx = ToolContext(sandbox_id, GATEWAY_URL)

    try:
        blocks = await run_turn(session, ctx, get_claude_client())
    except anthropic.APIError as e:
        raise HTTPException(502, f"LLM API error: {e}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Gateway error: {e}")
    finally:
        await ctx.close()

    return ChatResponse(session_id=session.id, blocks=blocks)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE streaming chat endpoint. Each event is a JSON block."""
    session = get_session(req.session_id)
    session.add_message("user", req.message)

    sandbox_id = await get_sandbox_id()
    ctx = ToolContext(sandbox_id, GATEWAY_URL)

    async def event_generator():
        try:
            async for block in run_turn_stream(session, ctx, get_claude_client()):
                yield f"data: {json.dumps(block, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        finally:
            await ctx.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/sessions", response_model=list[SessionInfo])
async def list_sessions():
    sessions = Session.list_sessions()
    return [
        SessionInfo(id=s["id"], preview=s["preview"], message_count=0)
        for s in sessions
    ]


@app.post("/sessions", response_model=SessionInfo)
async def create_session():
    session = Session()
    _sessions[session.id] = session
    return SessionInfo(id=session.id, preview="", message_count=0)


@app.get("/sessions/{session_id}")
async def get_session_history(session_id: str):
    session = get_session(session_id)
    return {"session_id": session.id, "messages": session.messages}
