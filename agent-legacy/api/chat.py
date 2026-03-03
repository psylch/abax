"""Chat routes — POST /chat (sync) and POST /chat/stream (SSE)."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from infra.auth import verify_api_key

from agent.core.orchestrator import run_turn, run_turn_streaming, make_sandbox_mgr
from agent.models import ChatRequest, ChatResponse
from agent.store import store

logger = logging.getLogger("abax.agent.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

Auth = Depends(verify_api_key)


async def _resolve_session(req: ChatRequest, user_id: str):
    """Get or create session + build SandboxManager. Returns (session, mgr)."""
    if req.session_id:
        session = await store.get_session(req.session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
    else:
        session = await store.create_session(user_id)

    mgr = make_sandbox_mgr(session["user_id"])
    if session.get("sandbox_id"):
        mgr.bind(session["sandbox_id"])
    return session, mgr


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest, caller: str | None = Auth):
    """Run a single agent turn. Creates a new session if session_id is not provided."""
    user_id = caller or req.user_id
    session, mgr = await _resolve_session(req, user_id)
    session_id = session["session_id"]

    try:
        history, _ = await asyncio.gather(
            store.load_history(session_id),
            store.save_message(session_id, "user", req.message),
        )

        result = await run_turn(
            req.message, session["user_id"], sandbox_mgr=mgr, history=history
        )

        await store.save_message(
            session_id, "assistant", result.text,
            tool_calls=result.tool_calls or None,
        )
        if result.sandbox_id and result.sandbox_id != session.get("sandbox_id"):
            await store.bind_sandbox(session_id, result.sandbox_id)

        await mgr.pause_if_active()

        return ChatResponse(
            session_id=session_id,
            text=result.text,
            tool_calls=result.tool_calls,
            sandbox_id=result.sandbox_id,
            cost_usd=result.cost_usd,
        )

    except Exception:
        logger.exception("Chat turn failed for session %s", session_id)
        raise HTTPException(500, "Agent turn failed")

    finally:
        await mgr.close()


@router.post("/stream")
async def chat_stream(req: ChatRequest, caller: str | None = Auth):
    """Run a single agent turn with real-time SSE streaming."""
    user_id = caller or req.user_id
    session, mgr = await _resolve_session(req, user_id)
    session_id = session["session_id"]

    history, _ = await asyncio.gather(
        store.load_history(session_id),
        store.save_message(session_id, "user", req.message),
    )

    async def sse_generator():
        try:
            full_text = ""
            tool_calls = []
            sandbox_id = None

            async for event in run_turn_streaming(
                req.message, session["user_id"],
                sandbox_mgr=mgr, history=history,
                session_id=session_id,
            ):
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"

                if event["event"] == "turn.end":
                    full_text = event["data"].get("text", "")
                    tool_calls = event["data"].get("tool_calls", [])
                    sandbox_id = event["data"].get("sandbox_id")

            # Persist results after stream completes
            await store.save_message(
                session_id, "assistant", full_text,
                tool_calls=tool_calls or None,
            )
            if sandbox_id and sandbox_id != session.get("sandbox_id"):
                await store.bind_sandbox(session_id, sandbox_id)

        except Exception:
            logger.exception("Streaming turn failed for session %s", session_id)
            yield f"event: error\ndata: {json.dumps({'error': 'Agent turn failed'})}\n\n"

        finally:
            # Always pause + close, even on client disconnect
            await mgr.pause_if_active()
            await mgr.close()

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
