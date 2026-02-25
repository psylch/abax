"""Test container garbage collection and enhanced health check."""
import pytest


@pytest.mark.asyncio
async def test_gc_cleans_exited_containers(client):
    """Create a container, stop it, run GC, verify it's removed."""
    r = await client.post("/sandboxes", json={"user_id": "test-gc"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    await client.post(f"/sandboxes/{sid}/stop")

    from infra.core.gc import collect_garbage
    removed = await collect_garbage(max_idle_seconds=0)
    assert sid in removed


@pytest.mark.asyncio
async def test_gc_preserves_active_running_containers(client):
    """Recently active running containers should not be removed by GC."""
    r = await client.post("/sandboxes", json={"user_id": "test-gc-keep"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    try:
        from infra.core.gc import collect_garbage
        # Use a large idle threshold — container was just created so it's "active"
        removed = await collect_garbage(max_idle_seconds=3600)
        assert sid not in removed
    finally:
        await client.delete(f"/sandboxes/{sid}")


@pytest.mark.asyncio
async def test_health_shows_docker_status(client):
    """Health endpoint should report Docker connectivity and image status."""
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["docker_connected"] is True
    assert data["sandbox_image_ready"] is True
    assert "active_sandboxes" in data
    assert data["status"] == "ok"
