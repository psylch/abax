"""Test warm pool functionality."""
import pytest


@pytest.mark.asyncio
async def test_pool_ensure_creates_containers():
    """ensure_pool should create containers up to POOL_SIZE."""
    from gateway import pool
    from gateway.sandbox import client, LABEL_PREFIX

    original = pool.POOL_SIZE
    pool.POOL_SIZE = 1

    try:
        # Clean any existing pool containers first
        for c in client.containers.list(
            filters={"label": f"{LABEL_PREFIX}.pool=true"}
        ):
            c.remove(force=True)

        created = await pool.ensure_pool()
        assert created >= 1

        count = await pool.warm_pool_count()
        assert count >= 1
    finally:
        # Cleanup pool containers
        for c in client.containers.list(
            filters={"label": f"{LABEL_PREFIX}.pool=true"}
        ):
            c.remove(force=True)
        pool.POOL_SIZE = original


@pytest.mark.asyncio
async def test_health_shows_pool_size(client):
    """Health endpoint should report warm_pool_size."""
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "warm_pool_size" in data
