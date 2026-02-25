"""Tests for Phase 6D: session-container binding, GC integration, context persistence.

Tests the full lifecycle:
- Session-container binding in store
- _ensure_sandbox uses session binding
- GC clears session bindings
- Context files persist across container destroy + recreate (via /data volume)
"""

import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from infra.core.store import SandboxStore
from infra.api.main import app


@pytest.fixture
def fresh_store(tmp_path):
    """A fresh SandboxStore with a temp DB for isolation."""
    return SandboxStore(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------------
# Store: session-container binding
# ---------------------------------------------------------------------------


class TestSessionContainerBinding:
    def test_bind_and_get(self, fresh_store):
        """bind_session_container stores the mapping, get_session_container retrieves it."""
        s = fresh_store
        s.create_session("user1", "test session")
        sessions = s.list_sessions("user1")
        sid = sessions[0]["session_id"]

        assert s.get_session_container(sid) is None

        s.bind_session_container(sid, "abc123")
        assert s.get_session_container(sid) == "abc123"

    def test_get_session_includes_sandbox_id(self, fresh_store):
        """get_session returns sandbox_id field."""
        s = fresh_store
        info = s.create_session("user1")
        sid = info["session_id"]

        session = s.get_session(sid)
        assert session["sandbox_id"] is None

        s.bind_session_container(sid, "xyz789")
        session = s.get_session(sid)
        assert session["sandbox_id"] == "xyz789"

    def test_list_sessions_includes_sandbox_id(self, fresh_store):
        """list_sessions returns sandbox_id field."""
        s = fresh_store
        info = s.create_session("user1")
        s.bind_session_container(info["session_id"], "container1")

        sessions = s.list_sessions("user1")
        assert sessions[0]["sandbox_id"] == "container1"

    def test_clear_session_container(self, fresh_store):
        """clear_session_container nullifies all sessions referencing a sandbox."""
        s = fresh_store
        s1 = s.create_session("user1")
        s2 = s.create_session("user1")
        s3 = s.create_session("user1")

        # Bind two sessions to same container
        s.bind_session_container(s1["session_id"], "container-a")
        s.bind_session_container(s2["session_id"], "container-a")
        s.bind_session_container(s3["session_id"], "container-b")

        # Clear container-a
        s.clear_session_container("container-a")

        assert s.get_session_container(s1["session_id"]) is None
        assert s.get_session_container(s2["session_id"]) is None
        # container-b unaffected
        assert s.get_session_container(s3["session_id"]) == "container-b"

    def test_clear_nonexistent_container(self, fresh_store):
        """clear_session_container is a no-op if no sessions reference the sandbox."""
        s = fresh_store
        s.clear_session_container("nonexistent")  # should not raise

    def test_rebind_session(self, fresh_store):
        """A session can be rebound to a different container."""
        s = fresh_store
        info = s.create_session("user1")
        sid = info["session_id"]

        s.bind_session_container(sid, "old-container")
        assert s.get_session_container(sid) == "old-container"

        s.bind_session_container(sid, "new-container")
        assert s.get_session_container(sid) == "new-container"

    def test_get_session_container_missing_session(self, fresh_store):
        """get_session_container returns None for nonexistent session."""
        assert fresh_store.get_session_container("nonexistent") is None


# ---------------------------------------------------------------------------
# Agent: _ensure_sandbox uses session binding
# ---------------------------------------------------------------------------


class TestEnsureSandboxBinding:
    async def test_ensure_sandbox_uses_bound_container(self):
        """_ensure_sandbox should prefer the session's bound container."""
        from gateway.agent import _ensure_sandbox

        mock_info = MagicMock(status="paused", sandbox_id="bound-123")

        with (
            patch("gateway.agent.store") as mock_store,
            patch("gateway.agent.get_sandbox", new_callable=AsyncMock, return_value=mock_info),
            patch("gateway.agent.resume_sandbox", new_callable=AsyncMock),
            patch("gateway.agent.emit_event", new_callable=AsyncMock),
        ):
            mock_store.get_session_container.return_value = "bound-123"

            sandbox_id, tier = await _ensure_sandbox("session-1", "user-1")

            assert sandbox_id == "bound-123"
            assert tier == "tier3"
            mock_store.get_session_container.assert_called_once_with("session-1")

    async def test_ensure_sandbox_creates_new_when_no_binding(self):
        """_ensure_sandbox creates new container when session has no binding."""
        from gateway.agent import _ensure_sandbox

        mock_info = MagicMock(sandbox_id="new-456")

        with (
            patch("gateway.agent.store") as mock_store,
            patch("gateway.agent._find_user_sandbox", new_callable=AsyncMock, return_value=None),
            patch("gateway.agent.create_sandbox", new_callable=AsyncMock, return_value=mock_info),
            patch("gateway.agent.emit_event", new_callable=AsyncMock),
        ):
            mock_store.get_session_container.return_value = None

            sandbox_id, tier = await _ensure_sandbox("session-1", "user-1")

            assert sandbox_id == "new-456"
            assert tier == "tier2"
            mock_store.bind_session_container.assert_called_once_with("session-1", "new-456")
            mock_store.register.assert_called_once_with("new-456", "user-1")

    async def test_ensure_sandbox_clears_stale_binding(self):
        """_ensure_sandbox clears binding if bound container is gone, then creates new."""
        from gateway.agent import _ensure_sandbox
        from docker.errors import NotFound

        mock_info = MagicMock(sandbox_id="fresh-789")

        with (
            patch("gateway.agent.store") as mock_store,
            patch("gateway.agent.get_sandbox", new_callable=AsyncMock, side_effect=NotFound("gone")),
            patch("gateway.agent._find_user_sandbox", new_callable=AsyncMock, return_value=None),
            patch("gateway.agent.create_sandbox", new_callable=AsyncMock, return_value=mock_info),
            patch("gateway.agent.emit_event", new_callable=AsyncMock),
        ):
            mock_store.get_session_container.return_value = "dead-container"

            sandbox_id, tier = await _ensure_sandbox("session-1", "user-1")

            assert sandbox_id == "fresh-789"
            assert tier == "tier2"
            # Stale binding was cleared
            mock_store.clear_session_container.assert_called_once_with("dead-container")
            # New binding was created
            mock_store.bind_session_container.assert_called_once_with("session-1", "fresh-789")

    async def test_ensure_sandbox_falls_back_to_user_container(self):
        """When session has no binding but user has a running container, use it."""
        from gateway.agent import _ensure_sandbox

        mock_info = MagicMock(status="running", sandbox_id="user-container")

        with (
            patch("gateway.agent.store") as mock_store,
            patch("gateway.agent.get_sandbox", new_callable=AsyncMock, return_value=mock_info),
            patch("gateway.agent._find_user_sandbox", new_callable=AsyncMock, return_value="user-container"),
            patch("gateway.agent.emit_event", new_callable=AsyncMock),
        ):
            mock_store.get_session_container.return_value = None

            sandbox_id, tier = await _ensure_sandbox("session-1", "user-1")

            assert sandbox_id == "user-container"
            assert tier == "tier3"
            mock_store.bind_session_container.assert_called_once_with("session-1", "user-container")


# ---------------------------------------------------------------------------
# GC: clears session bindings
# ---------------------------------------------------------------------------


class TestGCSessionCleanup:
    async def test_gc_clears_session_binding_on_exited(self):
        """GC should call clear_session_container when removing exited containers."""
        from infra.core.gc import collect_garbage

        mock_container = MagicMock()
        mock_container.id = "abcdef123456xxxxxx"  # longer than 12 chars like real Docker IDs
        mock_container.status = "exited"

        with (
            patch("infra.core.gc.store") as mock_store,
            patch("infra.core.gc.client") as mock_client,
        ):
            mock_store.all_sandbox_ids.return_value = []
            # Phase 2: exited containers
            mock_client.containers.list.side_effect = [
                [mock_container],  # exited
                [],  # paused
            ]
            mock_store.get_idle_sandboxes.return_value = []

            removed = await collect_garbage()

            assert removed == ["abcdef123456"]
            mock_store.clear_session_container.assert_any_call("abcdef123456")

    async def test_gc_clears_session_binding_on_idle(self):
        """GC should call clear_session_container when removing idle containers."""
        from infra.core.gc import collect_garbage

        mock_container = MagicMock()
        mock_container.id = "idle1234567890"
        mock_container.status = "running"

        with (
            patch("infra.core.gc.store") as mock_store,
            patch("infra.core.gc.client") as mock_client,
        ):
            mock_store.all_sandbox_ids.return_value = []
            mock_client.containers.list.side_effect = [
                [],  # exited
                [],  # paused
            ]
            mock_store.get_idle_sandboxes.return_value = ["idle12345678"]
            mock_client.containers.get.return_value = mock_container
            mock_container.reload.return_value = None

            removed = await collect_garbage()

            assert "idle12345678" in removed
            mock_store.clear_session_container.assert_any_call("idle12345678")


# ---------------------------------------------------------------------------
# Integration: session lifecycle via API
# ---------------------------------------------------------------------------


class TestSessionLifecycleAPI:
    async def test_session_shows_sandbox_id(self, client):
        """Session API should return sandbox_id field."""
        r = await client.post("/sessions", json={"user_id": "test-user"})
        assert r.status_code == 200
        data = r.json()
        assert "sandbox_id" in data
        assert data["sandbox_id"] is None

    async def test_session_list_shows_sandbox_id(self, client):
        """Session list should include sandbox_id."""
        await client.post("/sessions", json={"user_id": "lifecycle-user"})
        r = await client.get("/sessions", params={"user_id": "lifecycle-user"})
        assert r.status_code == 200
        sessions = r.json()
        assert len(sessions) >= 1
        assert "sandbox_id" in sessions[0]

    async def test_destroy_sandbox_clears_binding(self, client, sandbox_id):
        """Destroying a sandbox should clear session bindings."""
        from infra.core.store import store

        # Create a session and bind it
        r = await client.post("/sessions", json={"user_id": "test-user"})
        session = r.json()
        sid = session["session_id"]
        store.bind_session_container(sid, sandbox_id)

        assert store.get_session_container(sid) == sandbox_id

        # Destroy the sandbox (fixture cleanup will also try, but we do it explicitly)
        await client.delete(f"/sandboxes/{sandbox_id}")

        # Binding should be cleared
        assert store.get_session_container(sid) is None
