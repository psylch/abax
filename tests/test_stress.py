"""Stress tests — extreme scenarios to find breaking points.

Run separately: ABAX_POOL_SIZE=0 python -m pytest tests/test_stress.py -v
"""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.main import app
from gateway.sandbox import LABEL_PREFIX, client as docker_client
from sdk.sandbox import Sandbox


def _cleanup_all_stress_containers():
    """Remove ALL abax-managed containers — nuclear cleanup for test isolation."""
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
    """Ensure a clean state before and after each stress test."""
    _cleanup_all_stress_containers()
    yield
    _cleanup_all_stress_containers()


@pytest.fixture
async def stress_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=120) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Concurrent sandbox creation — per-user limit under race conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_creation_respects_per_user_limit(stress_client):
    """Blast 10 concurrent creates for same user — lock should enforce limit of 3."""
    tasks = [
        stress_client.post("/sandboxes", json={"user_id": "stress-user-1"})
        for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.status_code == 200]
    rejections = [r for r in results if r.status_code == 429]

    assert len(successes) == 3, f"Expected 3 successes, got {len(successes)}"
    assert len(rejections) == 7, f"Expected 7 rejections, got {len(rejections)}"


@pytest.mark.asyncio
async def test_concurrent_creation_multiple_users(stress_client):
    """3 users each creating 3 sandboxes concurrently = 9 total, under global limit of 10."""
    tasks = []
    for user_idx in range(3):
        for _ in range(3):
            tasks.append(
                stress_client.post("/sandboxes", json={"user_id": f"stress-multi-{user_idx}"})
            )

    results = await asyncio.gather(*tasks)
    successes = [r for r in results if r.status_code == 200]

    assert len(successes) == 9, f"Expected 9 successes, got {len(successes)}"


# ---------------------------------------------------------------------------
# 2. Rapid exec on same sandbox — concurrency stress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rapid_concurrent_exec(stress_client):
    """Fire 20 exec commands concurrently on the same sandbox."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-exec"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    tasks = [
        stress_client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": f"echo iteration-{i}", "timeout": 10},
        )
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if r.status_code == 200]
    assert len(successes) == 20, f"Expected 20 successes, got {len(successes)}"

    for i, r in enumerate(results):
        data = r.json()
        assert data["exit_code"] == 0
        assert f"iteration-{i}" in data["stdout"]


# ---------------------------------------------------------------------------
# 3. Large file operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_file_write_read(stress_client):
    """Write and read a 1MB file."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-file"})
    assert r.status_code == 200
    sb = Sandbox(r.json()["sandbox_id"], client=stress_client)

    # 1MB of text
    large_content = "A" * (1024 * 1024)
    await sb.files.write("/workspace/large.txt", large_content)

    content = await sb.files.read("/workspace/large.txt")
    assert len(content) == len(large_content)
    assert content == large_content


@pytest.mark.asyncio
async def test_many_small_files(stress_client):
    """Write 50 small files concurrently then list directory."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-manyfiles"})
    assert r.status_code == 200
    sb = Sandbox(r.json()["sandbox_id"], client=stress_client)

    tasks = [
        sb.files.write(f"/workspace/file_{i:03d}.txt", f"content-{i}")
        for i in range(50)
    ]
    await asyncio.gather(*tasks)

    entries = await sb.files.list("/workspace")
    names = {e["name"] for e in entries}
    for i in range(50):
        assert f"file_{i:03d}.txt" in names, f"Missing file_{i:03d}.txt"


# ---------------------------------------------------------------------------
# 4. Rapid pause/resume cycling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rapid_pause_resume_cycles(stress_client):
    """Pause and resume 10 times in a row — verify sandbox still works after."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-pause"})
    assert r.status_code == 200
    sb = Sandbox(r.json()["sandbox_id"], client=stress_client)

    for cycle in range(10):
        info = await sb.pause()
        assert info["status"] == "paused", f"Cycle {cycle}: pause failed"
        info = await sb.resume()
        assert info["status"] == "running", f"Cycle {cycle}: resume failed"

    # Verify sandbox still functional
    result = await sb.exec("echo alive")
    assert result["exit_code"] == 0
    assert "alive" in result["stdout"]


# ---------------------------------------------------------------------------
# 5. Create/destroy rapid cycling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rapid_create_destroy_cycles(stress_client):
    """Create and destroy 10 sandboxes sequentially — no leaks."""
    for i in range(10):
        r = await stress_client.post("/sandboxes", json={"user_id": "stress-cycle"})
        assert r.status_code == 200, f"Cycle {i}: create failed with {r.status_code}"
        sid = r.json()["sandbox_id"]

        r = await stress_client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": "echo ok"},
        )
        assert r.status_code == 200

        r = await stress_client.delete(f"/sandboxes/{sid}")
        assert r.status_code == 204

    # Verify no leaks
    r = await stress_client.get("/sandboxes")
    remaining = [s for s in r.json() if s["user_id"] == "stress-cycle"]
    assert len(remaining) == 0, f"Leaked {len(remaining)} sandboxes"


# ---------------------------------------------------------------------------
# 6. SSE events under load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_many_subscribers():
    """50 subscribers on one sandbox — all receive the event."""
    from gateway.events import EventBus

    eb = EventBus()
    queues = [eb.subscribe("stress-sse") for _ in range(50)]

    await eb.publish("stress-sse", "test.event", {"n": 42})

    for i, q in enumerate(queues):
        assert not q.empty(), f"Subscriber {i} missed the event"
        event = q.get_nowait()
        assert event["type"] == "test.event"

    for q in queues:
        eb.unsubscribe("stress-sse", q)


@pytest.mark.asyncio
async def test_sse_queue_overflow():
    """Publish 500 events — queue caps at 256, rest dropped gracefully."""
    from gateway.events import EventBus

    eb = EventBus()
    q = eb.subscribe("stress-rapid")

    for i in range(500):
        await eb.publish("stress-rapid", "rapid.event", {"seq": i})

    count = q.qsize()
    assert count == 256, f"Expected 256 events (queue cap), got {count}"

    first = q.get_nowait()
    assert first["data"]["seq"] == 0

    eb.unsubscribe("stress-rapid", q)


# ---------------------------------------------------------------------------
# 7. Exec timeout under concurrent load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_under_concurrent_load(stress_client):
    """Mix of fast and slow commands — fast ones complete, slow ones timeout."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-timeout"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    # 5 fast + 2 slow (timeout at 3s)
    fast_tasks = [
        stress_client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": f"echo fast-{i}", "timeout": 10},
        )
        for i in range(5)
    ]
    slow_tasks = [
        stress_client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": "sleep 30", "timeout": 3},
        )
        for _ in range(2)
    ]

    all_results = await asyncio.gather(*fast_tasks, *slow_tasks)

    fast_results = all_results[:5]
    slow_results = all_results[5:]

    for i, r in enumerate(fast_results):
        assert r.status_code == 200, f"Fast-{i} failed: {r.status_code}"
        assert r.json()["exit_code"] == 0

    for r in slow_results:
        # Either 504 (asyncio timeout) or 200 with non-zero exit (Linux timeout)
        assert r.status_code in (200, 504), f"Unexpected status: {r.status_code}"
        if r.status_code == 200:
            assert r.json()["exit_code"] != 0


# ---------------------------------------------------------------------------
# 8. Operations on destroyed sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operations_on_destroyed_sandbox(stress_client):
    """After destroy, all operations should return 404."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-ghost"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await stress_client.delete(f"/sandboxes/{sid}")

    checks = await asyncio.gather(
        stress_client.get(f"/sandboxes/{sid}"),
        stress_client.post(f"/sandboxes/{sid}/exec", json={"command": "echo hi"}),
        stress_client.post(f"/sandboxes/{sid}/stop"),
        stress_client.post(f"/sandboxes/{sid}/pause"),
        stress_client.post(f"/sandboxes/{sid}/resume"),
        stress_client.get(f"/sandboxes/{sid}/files/tmp/x"),
        stress_client.put(f"/sandboxes/{sid}/files/tmp/x", json={"content": "x", "path": "/tmp/x"}),
        stress_client.get(f"/sandboxes/{sid}/ls/tmp"),
    )
    for i, r in enumerate(checks):
        assert r.status_code == 404, f"Check {i}: expected 404, got {r.status_code}"


# ---------------------------------------------------------------------------
# 9. Double pause / double resume — 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_pause_returns_409(stress_client):
    """Pausing an already-paused sandbox → 409. Double resume → 409."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-double"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    r = await stress_client.post(f"/sandboxes/{sid}/pause")
    assert r.status_code == 200

    r = await stress_client.post(f"/sandboxes/{sid}/pause")
    assert r.status_code == 409

    r = await stress_client.post(f"/sandboxes/{sid}/resume")
    assert r.status_code == 200

    r = await stress_client.post(f"/sandboxes/{sid}/resume")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 10. Concurrent file writes to same path — no corruption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_file_writes_same_path(stress_client):
    """10 concurrent writes to the same file — result is one of the values, not corrupted."""
    r = await stress_client.post("/sandboxes", json={"user_id": "stress-race"})
    assert r.status_code == 200
    sb = Sandbox(r.json()["sandbox_id"], client=stress_client)

    tasks = [
        sb.files.write("/workspace/race.txt", f"writer-{i}")
        for i in range(10)
    ]
    await asyncio.gather(*tasks)

    content = await sb.files.read("/workspace/race.txt")
    assert content.startswith("writer-"), f"Unexpected content: {content!r}"
    writer_num = int(content.split("-")[1])
    assert 0 <= writer_num < 10
