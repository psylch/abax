"""Test sandbox pause/resume functionality."""
import pytest


@pytest.mark.asyncio
async def test_pause_and_resume(client, sandbox_id):
    """Pause a running sandbox, verify status, resume, verify running again."""
    # Pause
    r = await client.post(f"/sandboxes/{sandbox_id}/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"

    # Check status while paused
    r = await client.get(f"/sandboxes/{sandbox_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"

    # Resume
    r = await client.post(f"/sandboxes/{sandbox_id}/resume")
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    # Verify exec works after resume
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": "echo resumed"},
    )
    assert r.status_code == 200
    assert r.json()["stdout"].strip() == "resumed"


@pytest.mark.asyncio
async def test_pause_not_found(client):
    """Pausing a non-existent sandbox returns 404."""
    r = await client.post("/sandboxes/nonexistent/pause")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_resume_not_found(client):
    """Resuming a non-existent sandbox returns 404."""
    r = await client.post("/sandboxes/nonexistent/resume")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_gc_skips_paused_container(client):
    """GC should not remove a recently-paused container."""
    r = await client.post("/sandboxes", json={"user_id": "test-gc-pause"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    try:
        # Pause it
        await client.post(f"/sandboxes/{sid}/pause")

        from infra.core.gc import collect_garbage
        # Use a small idle threshold — but paused containers should be skipped
        removed = await collect_garbage(max_idle_seconds=0, max_pause_seconds=86400)
        assert sid not in removed

        # Container should still exist
        r = await client.get(f"/sandboxes/{sid}")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"
    finally:
        # Resume before cleanup (can't delete paused container easily)
        await client.post(f"/sandboxes/{sid}/resume")
        await client.delete(f"/sandboxes/{sid}")
