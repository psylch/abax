"""Session CRUD routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from agent.models import SessionInfo, MessageInfo
from agent.store import store
from infra.auth import verify_api_key

Auth = Depends(verify_api_key)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=list[SessionInfo])
async def list_sessions(user_id: str = Query(...), _=Auth):
    return await store.list_sessions(user_id)


@router.get("/{session_id}", response_model=SessionInfo)
async def get_session(session_id: str, _=Auth):
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


@router.get("/{session_id}/messages", response_model=list[MessageInfo])
async def get_messages(session_id: str, _=Auth):
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return await store.load_history(session_id)


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, _=Auth):
    deleted = await store.delete_session(session_id)
    if not deleted:
        raise HTTPException(404, "Session not found")
