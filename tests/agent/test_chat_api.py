"""API-level tests for /chat and /chat/stream endpoints."""

import json
from unittest.mock import patch, AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from agent.core.orchestrator import TurnResult
from tests.agent.mock_sdk import mock_query


def _make_turn_result(text="hi there", **kwargs):
    defaults = dict(
        text=text,
        tool_calls=[],
        sandbox_id=None,
        cost_usd=0.01,
        num_turns=1,
    )
    defaults.update(kwargs)
    return TurnResult(**defaults)


def _mock_mgr():
    mgr = AsyncMock()
    mgr.sandbox_id = None
    return mgr


@pytest.mark.asyncio
async def test_chat_new_session(client):
    """POST /chat without session_id creates a new session and returns text."""
    with patch("agent.api.chat.run_turn", new_callable=AsyncMock) as mock_run, \
         patch("agent.api.chat.make_sandbox_mgr", return_value=_mock_mgr()):
        mock_run.return_value = _make_turn_result()

        resp = await client.post("/chat", json={"message": "hello", "user_id": "u1"})

    assert resp.status_code == 200
    body = resp.json()
    assert "session_id" in body
    assert body["text"] == "hi there"


@pytest.mark.asyncio
async def test_chat_existing_session(client):
    """POST /chat with a valid session_id reuses that session."""
    # First create a session
    with patch("agent.api.chat.run_turn", new_callable=AsyncMock) as mock_run, \
         patch("agent.api.chat.make_sandbox_mgr", return_value=_mock_mgr()):
        mock_run.return_value = _make_turn_result()
        resp1 = await client.post("/chat", json={"message": "first", "user_id": "u1"})
        sid = resp1.json()["session_id"]

    # Reuse the session
    with patch("agent.api.chat.run_turn", new_callable=AsyncMock) as mock_run, \
         patch("agent.api.chat.make_sandbox_mgr", return_value=_mock_mgr()):
        mock_run.return_value = _make_turn_result(text="second reply")
        resp2 = await client.post("/chat", json={"message": "second", "user_id": "u1", "session_id": sid})

    assert resp2.status_code == 200
    assert resp2.json()["session_id"] == sid
    assert resp2.json()["text"] == "second reply"


@pytest.mark.asyncio
async def test_chat_session_not_found(client):
    """POST /chat with a non-existent session_id returns 404."""
    resp = await client.post("/chat", json={
        "message": "hello",
        "user_id": "u1",
        "session_id": "nonexistent-session-id",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_events(client):
    """POST /chat/stream returns SSE with turn.start and turn.end events."""
    responses = [
        AssistantMessage(content=[TextBlock("streamed")], model="mock"),
    ]

    with patch("agent.api.chat.run_turn_streaming") as mock_stream, \
         patch("agent.api.chat.make_sandbox_mgr", return_value=_mock_mgr()):

        async def fake_streaming(*args, **kwargs):
            yield {"event": "turn.start", "data": {"session_id": "s1"}}
            yield {"event": "text.delta", "data": {"text": "streamed"}}
            yield {"event": "turn.end", "data": {
                "text": "streamed", "tool_calls": [], "sandbox_id": None,
                "cost_usd": 0.01, "num_turns": 1,
            }}

        mock_stream.return_value = fake_streaming()

        resp = await client.post("/chat/stream", json={"message": "hello", "user_id": "u1"})

    assert resp.status_code == 200
    body = resp.text
    assert "event: turn.start" in body
    assert "event: turn.end" in body
    assert "event: text.delta" in body


@pytest.mark.asyncio
async def test_stream_persists_messages(client):
    """After streaming, GET /sessions/{id}/messages has user + assistant messages."""
    with patch("agent.api.chat.run_turn_streaming") as mock_stream, \
         patch("agent.api.chat.make_sandbox_mgr", return_value=_mock_mgr()):

        async def fake_streaming(*args, **kwargs):
            yield {"event": "turn.start", "data": {"session_id": "s1"}}
            yield {"event": "turn.end", "data": {
                "text": "reply", "tool_calls": [], "sandbox_id": None,
                "cost_usd": 0.01, "num_turns": 1,
            }}

        mock_stream.return_value = fake_streaming()

        # Create session first via a non-stream call so we have a session_id
        with patch("agent.api.chat.run_turn", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _make_turn_result()
            resp1 = await client.post("/chat", json={"message": "setup", "user_id": "u1"})
            sid = resp1.json()["session_id"]

        # Now stream into the same session
        mock_stream.return_value = fake_streaming()
        resp2 = await client.post("/chat/stream", json={
            "message": "stream msg", "user_id": "u1", "session_id": sid,
        })
        assert resp2.status_code == 200
        # Consume the response to ensure the SSE generator finishes
        _ = resp2.text

    # Check messages: setup(user) + setup(assistant) + stream(user) + stream(assistant) = 4
    resp3 = await client.get(f"/sessions/{sid}/messages")
    assert resp3.status_code == 200
    messages = resp3.json()
    roles = [m["role"] for m in messages]
    assert roles.count("user") >= 2
    assert roles.count("assistant") >= 2
