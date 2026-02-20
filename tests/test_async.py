"""Test that gateway operations don't block the event loop."""
import asyncio
import pytest
from tests.conftest import _wait_for_daemon


@pytest.mark.asyncio
async def test_concurrent_health_during_exec(client):
    """Health check should respond even while exec is running."""
    r = await client.post("/sandboxes", json={"user_id": "test-async"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]

    try:
        await _wait_for_daemon(sid)
        slow_exec = client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": "sleep 2 && echo done", "timeout": 10},
        )
        health = client.get("/health")

        results = await asyncio.gather(health, slow_exec)
        health_r, exec_r = results

        assert health_r.status_code == 200
        assert exec_r.status_code == 200
        assert exec_r.json()["stdout"].strip() == "done"
    finally:
        await client.delete(f"/sandboxes/{sid}")
