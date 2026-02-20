"""Multi-tenant stress tests — concurrent sessions, JWT auth, Tier routing, binding races.

Run separately: ABAX_POOL_SIZE=0 python -m pytest tests/test_stress_multitenant.py -v
"""
import asyncio
import time
import uuid
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.main import app
from gateway.store import SandboxStore
from gateway.sandbox import LABEL_PREFIX, client as docker_client
from gateway.auth import create_jwt
from tests.conftest import _wait_for_daemon


def _cleanup_all_containers():
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"{LABEL_PREFIX}.managed=true"},
    )
    for c in containers:
        try:
            c.remove(force=True)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def clean_slate():
    _cleanup_all_containers()
    yield
    _cleanup_all_containers()


@pytest.fixture
async def mt_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=120) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Concurrent session creation — same user, many sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_session_creation_same_user(mt_client):
    """20 concurrent session creates for same user — all should succeed (no limit on sessions)."""
    tasks = [
        mt_client.post("/sessions", json={"user_id": "stress-user-1"})
        for _ in range(20)
    ]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 20, f"Expected 20 successes, got {len(successes)}"

    # Verify all session_ids are unique
    session_ids = {r.json()["session_id"] for r in successes}
    assert len(session_ids) == 20, "Session IDs should be unique"


@pytest.mark.asyncio
async def test_concurrent_session_creation_many_users(mt_client):
    """10 users each creating 5 sessions concurrently = 50 total."""
    # Use unique prefix to avoid cross-test pollution (global SQLite singleton)
    run_id = uuid.uuid4().hex[:8]
    tasks = []
    for user_idx in range(10):
        for _ in range(5):
            tasks.append(
                mt_client.post(
                    "/sessions",
                    json={"user_id": f"mt-{run_id}-{user_idx}"},
                )
            )

    results = await asyncio.gather(*tasks)
    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 50

    # Verify per-user counts
    for user_idx in range(10):
        r = await mt_client.get(
            "/sessions", params={"user_id": f"mt-{run_id}-{user_idx}"}
        )
        sessions = r.json()
        assert len(sessions) == 5, f"User {user_idx} expected 5 sessions, got {len(sessions)}"


# ---------------------------------------------------------------------------
# 2. Concurrent message saves — same session, many writers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_message_saves_same_session(mt_client):
    """20 concurrent message saves to the same session — all should succeed, correct order."""
    r = await mt_client.post("/sessions", json={"user_id": "stress-msg"})
    sid = r.json()["session_id"]

    tasks = [
        mt_client.post(
            f"/sessions/{sid}/messages",
            json={"role": "user", "content": f"message-{i}"},
        )
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 20

    # Verify all messages are stored
    r = await mt_client.get(f"/sessions/{sid}/history")
    messages = r.json()["messages"]
    assert len(messages) == 20

    # Messages should all be present (order may vary due to concurrency)
    contents = {m["content"] for m in messages}
    for i in range(20):
        assert f"message-{i}" in contents


# ---------------------------------------------------------------------------
# 3. JWT auth under concurrency — many users authenticating simultaneously
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_jwt_auth(mt_client, monkeypatch):
    """20 users simultaneously authenticating with different JWTs."""
    monkeypatch.setenv("ABAX_API_KEY", "stress-api-key")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "stress-jwt-secret")

    async def auth_request(user_idx):
        token = create_jwt(f"jwt-user-{user_idx}")
        return await mt_client.post(
            "/sessions",
            json={"user_id": f"jwt-user-{user_idx}"},
            headers={"Authorization": f"Bearer {token}"},
        )

    tasks = [auth_request(i) for i in range(20)]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 20


@pytest.mark.asyncio
async def test_mixed_auth_concurrent(mt_client, monkeypatch):
    """Mix of JWT and API key auth requests concurrently."""
    monkeypatch.setenv("ABAX_API_KEY", "stress-key")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "stress-secret")

    async def jwt_request(i):
        token = create_jwt(f"jwt-user-{i}")
        return await mt_client.post(
            "/sessions",
            json={"user_id": f"jwt-user-{i}"},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def apikey_request(i):
        return await mt_client.post(
            "/sessions",
            json={"user_id": f"key-user-{i}"},
            headers={"Authorization": "Bearer stress-key"},
        )

    tasks = [jwt_request(i) for i in range(10)] + [apikey_request(i) for i in range(10)]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 20


@pytest.mark.asyncio
async def test_invalid_jwt_concurrent(mt_client, monkeypatch):
    """Concurrent invalid JWT requests should all return 401."""
    monkeypatch.setenv("ABAX_API_KEY", "real-key")
    monkeypatch.setattr("gateway.auth.JWT_SECRET", "real-secret")

    tasks = [
        mt_client.post(
            "/sessions",
            json={"user_id": f"bad-user-{i}"},
            headers={"Authorization": f"Bearer garbage-token-{i}"},
        )
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    rejections = [r for r in results if r.status_code == 401]
    assert len(rejections) == 20


# ---------------------------------------------------------------------------
# 4. Session-container binding races
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_bind_same_session():
    """Multiple concurrent binds to the same session — last write wins, no crash."""
    store = SandboxStore(db_path="/tmp/abax-stress-bind.db")
    info = store.create_session("user-1")
    sid = info["session_id"]

    async def bind(container_id):
        await asyncio.to_thread(store.bind_session_container, sid, container_id)

    tasks = [bind(f"container-{i}") for i in range(20)]
    await asyncio.gather(*tasks)

    # Result should be one of the container IDs
    result = store.get_session_container(sid)
    assert result is not None
    assert result.startswith("container-")


@pytest.mark.asyncio
async def test_concurrent_clear_and_bind():
    """Concurrent bind and clear operations — no deadlock or crash."""
    store = SandboxStore(db_path="/tmp/abax-stress-clear.db")
    sessions = [store.create_session("user-1") for _ in range(10)]

    # Bind all to same container
    for s in sessions:
        store.bind_session_container(s["session_id"], "shared-container")

    async def clear():
        await asyncio.to_thread(store.clear_session_container, "shared-container")

    async def rebind(idx):
        await asyncio.to_thread(
            store.bind_session_container,
            sessions[idx]["session_id"],
            f"new-container-{idx}",
        )

    # Fire clears and rebinds concurrently
    tasks = [clear()] + [rebind(i) for i in range(10)]
    await asyncio.gather(*tasks)

    # No crash = success. Check that results are consistent
    for i, s in enumerate(sessions):
        result = store.get_session_container(s["session_id"])
        # Either None (cleared) or new-container-N (rebound)
        assert result is None or result.startswith("new-container-")


# ---------------------------------------------------------------------------
# 5. Multi-user sandbox + session interleaving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_user_sandbox_session_interleave(mt_client):
    """3 users each create a session + sandbox, then interleave operations."""
    users = []
    for i in range(3):
        uid = f"interleave-user-{i}"
        # Create session
        r = await mt_client.post("/sessions", json={"user_id": uid})
        assert r.status_code == 200
        session = r.json()

        # Create sandbox
        r = await mt_client.post("/sandboxes", json={"user_id": uid})
        assert r.status_code == 200
        sandbox = r.json()
        await _wait_for_daemon(sandbox["sandbox_id"])

        users.append({"uid": uid, "session": session, "sandbox": sandbox})

    # Concurrent operations: each user writes a file and saves a message
    async def user_ops(user_data):
        sid = user_data["sandbox"]["sandbox_id"]
        sess_id = user_data["session"]["session_id"]
        uid = user_data["uid"]

        # Write file
        r = await mt_client.put(
            f"/sandboxes/{sid}/files/workspace/identity.txt",
            json={"content": f"I am {uid}", "path": "/workspace/identity.txt"},
        )
        assert r.status_code == 200

        # Save message
        r = await mt_client.post(
            f"/sessions/{sess_id}/messages",
            json={"role": "user", "content": f"Hello from {uid}"},
        )
        assert r.status_code == 200

        return uid

    results = await asyncio.gather(*[user_ops(u) for u in users])
    assert len(results) == 3

    # Verify isolation: each user's file has their own identity
    for u in users:
        r = await mt_client.get(
            f"/sandboxes/{u['sandbox']['sandbox_id']}/files/workspace/identity.txt"
        )
        assert r.status_code == 200
        assert r.json()["content"] == f"I am {u['uid']}"

    # Verify isolation: each user's session has their own message
    for u in users:
        r = await mt_client.get(
            f"/sessions/{u['session']['session_id']}/history"
        )
        assert r.status_code == 200
        messages = r.json()["messages"]
        assert len(messages) == 1
        assert u["uid"] in messages[0]["content"]


# ---------------------------------------------------------------------------
# 6. Session history under high volume — many messages, many sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_volume_message_storage(mt_client):
    """Single session with 100 messages — history stays consistent."""
    r = await mt_client.post("/sessions", json={"user_id": "bulk-msg-user"})
    sid = r.json()["session_id"]

    # Save 100 messages sequentially (realistic for a long conversation)
    for i in range(100):
        role = "user" if i % 2 == 0 else "assistant"
        r = await mt_client.post(
            f"/sessions/{sid}/messages",
            json={"role": role, "content": f"msg-{i:03d}"},
        )
        assert r.status_code == 200

    # Load history
    r = await mt_client.get(f"/sessions/{sid}/history")
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert len(messages) == 100

    # Verify order is preserved
    for i, msg in enumerate(messages):
        expected_role = "user" if i % 2 == 0 else "assistant"
        assert msg["role"] == expected_role
        assert msg["content"] == f"msg-{i:03d}"


@pytest.mark.asyncio
async def test_many_sessions_list_performance(mt_client):
    """Create 50 sessions for one user — listing should return all efficiently."""
    uid = f"perf-user-{uuid.uuid4().hex[:8]}"
    for i in range(50):
        r = await mt_client.post(
            "/sessions", json={"user_id": uid, "title": f"Session {i}"}
        )
        assert r.status_code == 200

    start = time.monotonic()
    r = await mt_client.get("/sessions", params={"user_id": uid})
    elapsed = time.monotonic() - start

    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 50
    # Should be fast (SQLite, <100ms even for 50 sessions)
    assert elapsed < 1.0, f"Session listing took {elapsed:.2f}s, should be <1s"


# ---------------------------------------------------------------------------
# 7. Tier routing under concurrency (mocked LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_tier1_chat_no_containers(mt_client):
    """5 users chatting concurrently in Tier 1 — zero containers should be created."""
    # Create sessions for 5 users
    sessions = []
    for i in range(5):
        r = await mt_client.post("/sessions", json={"user_id": f"tier1-user-{i}"})
        sessions.append(r.json())

    # Mock LLM to always return text (Tier 1)
    mock_response = {
        "content": [{"type": "text", "text": "Hello! I can help with that."}],
        "model": "test",
        "role": "assistant",
    }

    with patch("gateway.agent.proxy_llm_request", new_callable=AsyncMock, return_value=mock_response):
        async def chat(session):
            return await mt_client.post(
                f"/sessions/{session['session_id']}/chat",
                json={"message": "Hi there"},
            )

        results = await asyncio.gather(*[chat(s) for s in sessions])

    # All should succeed with tier1
    for r in results:
        assert r.status_code == 200
        data = r.json()
        assert data["tier"] == "tier1"
        assert data["sandbox_id"] is None

    # Verify no containers were created
    r = await mt_client.get("/sandboxes")
    containers = [s for s in r.json() if s["user_id"].startswith("tier1-user-")]
    assert len(containers) == 0, f"Expected 0 containers, got {len(containers)}"


@pytest.mark.asyncio
async def test_concurrent_tier2_chat_creates_containers(mt_client):
    """3 users chatting with tool_use — each gets their own container."""
    sessions = []
    for i in range(3):
        r = await mt_client.post("/sessions", json={"user_id": f"tier2-user-{i}"})
        sessions.append(r.json())

    # Mock LLM to return tool_use (triggers Tier 2)
    mock_first_response = {
        "content": [
            {"type": "text", "text": "Let me check."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "execute_command",
                "input": {"command": "echo hello"},
            },
        ],
        "model": "test",
        "role": "assistant",
    }

    # Mock daemon turn to return immediately
    mock_daemon_result = {
        "response": "Done!",
        "turns": [],
        "tool_calls_count": 1,
    }

    with (
        patch("gateway.agent.proxy_llm_request", new_callable=AsyncMock, return_value=mock_first_response),
        patch("gateway.agent._run_daemon_turn", new_callable=AsyncMock, return_value=mock_daemon_result),
        patch("gateway.agent._wait_for_daemon_ready", new_callable=AsyncMock),
    ):
        async def chat(session):
            return await mt_client.post(
                f"/sessions/{session['session_id']}/chat",
                json={"message": "Run something"},
            )

        results = await asyncio.gather(*[chat(s) for s in sessions])

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 3

    # Each user should get a different sandbox
    sandbox_ids = {r.json()["sandbox_id"] for r in successes}
    assert len(sandbox_ids) == 3, f"Expected 3 unique sandboxes, got {len(sandbox_ids)}"

    # All should be tier2
    for r in successes:
        assert r.json()["tier"] == "tier2"


@pytest.mark.asyncio
async def test_same_user_multiple_sessions_share_container(mt_client):
    """Same user with 2 sessions — second session should reuse the container (Tier 3)."""
    uid = "share-user"
    r1 = await mt_client.post("/sessions", json={"user_id": uid})
    r2 = await mt_client.post("/sessions", json={"user_id": uid})
    session1 = r1.json()
    session2 = r2.json()

    mock_first_response = {
        "content": [
            {"type": "tool_use", "id": "t1", "name": "execute_command", "input": {"command": "echo hi"}},
        ],
        "model": "test",
        "role": "assistant",
    }
    mock_daemon_result = {"response": "Ok", "turns": [], "tool_calls_count": 1}

    with (
        patch("gateway.agent.proxy_llm_request", new_callable=AsyncMock, return_value=mock_first_response),
        patch("gateway.agent._run_daemon_turn", new_callable=AsyncMock, return_value=mock_daemon_result),
        patch("gateway.agent._wait_for_daemon_ready", new_callable=AsyncMock),
    ):
        # First session creates a container (Tier 2)
        r = await mt_client.post(
            f"/sessions/{session1['session_id']}/chat",
            json={"message": "Do work"},
        )
        assert r.status_code == 200
        first_sandbox = r.json()["sandbox_id"]
        assert r.json()["tier"] == "tier2"

        # Second session should find the paused container (Tier 3)
        r = await mt_client.post(
            f"/sessions/{session2['session_id']}/chat",
            json={"message": "Do more work"},
        )
        assert r.status_code == 200
        second_sandbox = r.json()["sandbox_id"]
        assert r.json()["tier"] == "tier3"

        # Same container
        assert first_sandbox == second_sandbox


# ---------------------------------------------------------------------------
# 8. Context file isolation between users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_isolation_between_users(tmp_path, monkeypatch):
    """Each user's context files are isolated — no cross-read."""
    monkeypatch.setattr("gateway.context.PERSISTENT_ROOT", str(tmp_path))

    # Create context for two users
    for uid in ("user-a", "user-b"):
        ctx_dir = tmp_path / uid / "context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "memory.md").write_text(f"I am {uid}")

    from gateway.context import read_user_context

    ctx_a = read_user_context("user-a")
    ctx_b = read_user_context("user-b")

    assert ctx_a["memory.md"] == "I am user-a"
    assert ctx_b["memory.md"] == "I am user-b"

    # Non-existent user gets empty context
    ctx_c = read_user_context("user-c")
    assert ctx_c == {}


@pytest.mark.asyncio
async def test_context_path_traversal_blocked(tmp_path, monkeypatch):
    """Path traversal in user_id should return empty context, not leak files."""
    monkeypatch.setattr("gateway.context.PERSISTENT_ROOT", str(tmp_path))

    # Create a file outside the persistent root
    (tmp_path.parent / "secret.md").write_text("top secret")

    from gateway.context import read_user_context

    # Try path traversal
    result = read_user_context("../../")
    assert result == {}

    result = read_user_context("../")
    assert result == {}


# ---------------------------------------------------------------------------
# 9. Store concurrency — SQLite under parallel writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_concurrent_writes():
    """50 concurrent writes to the same SQLite DB — no data loss."""
    store = SandboxStore(db_path="/tmp/abax-stress-sqlite.db")
    sess = store.create_session("user-1", "stress test")
    sid = sess["session_id"]

    async def save_msg(i):
        await asyncio.to_thread(
            store.save_message, sid, "user", f"concurrent-msg-{i}"
        )

    await asyncio.gather(*[save_msg(i) for i in range(50)])

    history = store.load_history(sid)
    assert len(history) == 50

    contents = {m["content"] for m in history}
    for i in range(50):
        assert f"concurrent-msg-{i}" in contents


@pytest.mark.asyncio
async def test_sqlite_concurrent_session_and_messages():
    """Create sessions and save messages concurrently across multiple users."""
    store = SandboxStore(db_path="/tmp/abax-stress-mixed.db")

    async def user_workflow(user_idx):
        sess = await asyncio.to_thread(
            store.create_session, f"user-{user_idx}", f"Session for user {user_idx}"
        )
        sid = sess["session_id"]
        for j in range(10):
            await asyncio.to_thread(
                store.save_message, sid, "user", f"user-{user_idx}-msg-{j}"
            )
        return sid

    session_ids = await asyncio.gather(*[user_workflow(i) for i in range(10)])

    # Verify each user's messages
    for i, sid in enumerate(session_ids):
        history = store.load_history(sid)
        assert len(history) == 10
        for j, msg in enumerate(history):
            assert msg["content"] == f"user-{i}-msg-{j}"


# ---------------------------------------------------------------------------
# 10. Sandbox destroy with active session binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_sandbox_with_many_bound_sessions(mt_client):
    """Destroy a sandbox that has 10 sessions bound to it — all bindings cleared."""
    from gateway.store import store

    # Create sandbox
    r = await mt_client.post("/sandboxes", json={"user_id": "destroy-user"})
    assert r.status_code == 200
    sandbox_id = r.json()["sandbox_id"]

    # Create 10 sessions and bind them all
    session_ids = []
    for i in range(10):
        r = await mt_client.post(
            "/sessions", json={"user_id": "destroy-user", "title": f"S{i}"}
        )
        sid = r.json()["session_id"]
        store.bind_session_container(sid, sandbox_id)
        session_ids.append(sid)

    # Verify bindings exist
    for sid in session_ids:
        assert store.get_session_container(sid) == sandbox_id

    # Destroy sandbox
    r = await mt_client.delete(f"/sandboxes/{sandbox_id}")
    assert r.status_code == 204

    # All bindings should be cleared
    for sid in session_ids:
        assert store.get_session_container(sid) is None, f"Binding not cleared for {sid}"

    # Sessions themselves should still exist
    for sid in session_ids:
        r = await mt_client.get(f"/sessions/{sid}")
        assert r.status_code == 200
