"""Tests for session CRUD, message storage, JWT auth, and user context."""

import os
import time
import tempfile

import pytest
from httpx import AsyncClient, ASGITransport

from gateway.store import SandboxStore
from gateway.auth import create_jwt, decode_jwt


# --- Store-level tests (no HTTP) ---


@pytest.fixture
def session_store(tmp_path):
    """Create a fresh SandboxStore with a temp DB for session tests."""
    db = str(tmp_path / "test.db")
    return SandboxStore(db_path=db)


def test_create_session(session_store):
    result = session_store.create_session("user-1", title="Test Session")
    assert result["user_id"] == "user-1"
    assert result["title"] == "Test Session"
    assert "session_id" in result
    assert result["created_at"] > 0


def test_get_session(session_store):
    created = session_store.create_session("user-1")
    fetched = session_store.get_session(created["session_id"])
    assert fetched is not None
    assert fetched["session_id"] == created["session_id"]
    assert fetched["user_id"] == "user-1"


def test_get_session_not_found(session_store):
    assert session_store.get_session("nonexistent") is None


def test_list_sessions(session_store):
    session_store.create_session("user-1", title="S1")
    session_store.create_session("user-1", title="S2")
    session_store.create_session("user-2", title="S3")
    sessions = session_store.list_sessions("user-1")
    assert len(sessions) == 2
    assert all(s["user_id"] == "user-1" for s in sessions)


def test_save_and_load_messages(session_store):
    sess = session_store.create_session("user-1")
    sid = sess["session_id"]
    session_store.save_message(sid, "user", "Hello")
    session_store.save_message(sid, "assistant", "Hi there")
    session_store.save_message(sid, "user", "Do something", tool_calls='[{"name":"exec"}]')
    history = session_store.load_history(sid)
    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello"
    assert history[1]["role"] == "assistant"
    assert history[2]["tool_calls"] == '[{"name":"exec"}]'


def test_save_message_updates_session_last_active(session_store):
    sess = session_store.create_session("user-1")
    sid = sess["session_id"]
    original = sess["last_active_at"]
    time.sleep(0.05)
    session_store.save_message(sid, "user", "Hello")
    updated = session_store.get_session(sid)
    assert updated["last_active_at"] > original


def test_load_history_empty(session_store):
    sess = session_store.create_session("user-1")
    assert session_store.load_history(sess["session_id"]) == []


# --- JWT tests ---


def test_create_and_decode_jwt(monkeypatch):
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "test-secret-key")
    token = create_jwt("user-42")
    payload = decode_jwt(token)
    assert payload is not None
    assert payload["sub"] == "user-42"
    assert "exp" in payload


def test_decode_jwt_invalid_token(monkeypatch):
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "test-secret-key")
    assert decode_jwt("garbage.token.here") is None


def test_decode_jwt_wrong_secret(monkeypatch):
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "secret-a")
    token = create_jwt("user-1")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "secret-b")
    assert decode_jwt(token) is None


def test_decode_jwt_no_secret(monkeypatch):
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "")
    assert decode_jwt("any.token.here") is None


def test_create_jwt_expired(monkeypatch):
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "test-secret-key")
    import jwt as pyjwt
    payload = {
        "sub": "user-1",
        "exp": int(time.time()) - 100,
        "iat": int(time.time()) - 200,
    }
    token = pyjwt.encode(payload, "test-secret-key", algorithm="HS256")
    assert decode_jwt(token) is None


# --- HTTP route tests ---


@pytest.fixture
async def client():
    """Standalone client for session tests (no Docker needed)."""
    from gateway.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_session_create_route(client):
    r = await client.post("/sessions", json={"user_id": "test-user"})
    assert r.status_code == 200
    data = r.json()
    assert data["user_id"] == "test-user"
    assert "session_id" in data


async def test_session_create_with_title(client):
    r = await client.post("/sessions", json={"user_id": "test-user", "title": "My Chat"})
    assert r.status_code == 200
    assert r.json()["title"] == "My Chat"


async def test_session_list_route(client):
    await client.post("/sessions", json={"user_id": "list-user"})
    await client.post("/sessions", json={"user_id": "list-user"})
    r = await client.get("/sessions", params={"user_id": "list-user"})
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) >= 2


async def test_session_get_route(client):
    cr = await client.post("/sessions", json={"user_id": "test-user"})
    sid = cr.json()["session_id"]
    r = await client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["session_id"] == sid


async def test_session_get_not_found(client):
    r = await client.get("/sessions/nonexistent-id")
    assert r.status_code == 404


async def test_message_save_and_history(client):
    cr = await client.post("/sessions", json={"user_id": "test-user"})
    sid = cr.json()["session_id"]
    await client.post(f"/sessions/{sid}/messages", json={"role": "user", "content": "Hello"})
    await client.post(f"/sessions/{sid}/messages", json={"role": "assistant", "content": "Hi"})
    r = await client.get(f"/sessions/{sid}/history")
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == sid
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"


async def test_message_save_to_nonexistent_session(client):
    r = await client.post("/sessions/nonexistent/messages", json={"role": "user", "content": "Hello"})
    assert r.status_code == 404


async def test_history_for_nonexistent_session(client):
    r = await client.get("/sessions/nonexistent/history")
    assert r.status_code == 404


async def test_auth_jwt_route(client, monkeypatch):
    """JWT auth should allow access to session routes."""
    monkeypatch.setenv("ABAX_API_KEY", "real-api-key")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "test-jwt-secret")
    token = create_jwt("jwt-user")
    r = await client.post(
        "/sessions",
        json={"user_id": "jwt-user"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


async def test_auth_api_key_fallback(client, monkeypatch):
    """API key should still work when JWT_SECRET is set."""
    monkeypatch.setenv("ABAX_API_KEY", "my-api-key")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "test-jwt-secret")
    r = await client.post(
        "/sessions",
        json={"user_id": "test-user"},
        headers={"Authorization": "Bearer my-api-key"},
    )
    assert r.status_code == 200


async def test_auth_bad_token_rejected(client, monkeypatch):
    """Invalid token should be rejected when ABAX_API_KEY is set."""
    monkeypatch.setenv("ABAX_API_KEY", "real-api-key")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "test-jwt-secret")
    r = await client.post(
        "/sessions",
        json={"user_id": "test-user"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


# --- User context tests ---


def test_read_user_context(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.context.PERSISTENT_ROOT", str(tmp_path))
    ctx_dir = tmp_path / "user-1" / "context"
    ctx_dir.mkdir(parents=True)
    (ctx_dir / "prefs.md").write_text("I like Python")
    (ctx_dir / "notes.md").write_text("Some notes")
    (ctx_dir / "ignore.txt").write_text("Not a markdown file")

    from gateway.context import read_user_context
    result = read_user_context("user-1")
    assert len(result) == 2
    assert result["prefs.md"] == "I like Python"
    assert result["notes.md"] == "Some notes"


def test_read_user_context_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.context.PERSISTENT_ROOT", str(tmp_path))
    from gateway.context import read_user_context
    result = read_user_context("no-such-user")
    assert result == {}
