"""Tests for exec timeout enforcement."""
import pytest


@pytest.mark.asyncio
async def test_exec_within_timeout(client, sandbox_id):
    """Normal command completes within timeout."""
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": "echo ok", "timeout": 10},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "ok"


@pytest.mark.asyncio
async def test_exec_timeout_kills_command(client, sandbox_id):
    """Command exceeding timeout is killed; exit_code == 124 (timeout's convention)."""
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": "sleep 60", "timeout": 2},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["exit_code"] == 124


@pytest.mark.asyncio
async def test_exec_timeout_partial_output(client, sandbox_id):
    """Partial stdout produced before timeout is still returned."""
    # Print lines then sleep forever; timeout will kill it but we should see output
    cmd = "echo before_timeout; sleep 60"
    r = await client.post(
        f"/sandboxes/{sandbox_id}/exec",
        json={"command": cmd, "timeout": 2},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["exit_code"] == 124
    assert "before_timeout" in result["stdout"]
