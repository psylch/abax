"""Test the Python SDK against the gateway (via ASGI transport)."""
import pytest
import httpx
from httpx import ASGITransport
from infra.api.main import app
from sdk.sandbox import Sandbox
from tests.infra.conftest import _wait_for_daemon


@pytest.fixture
async def sdk_sandbox():
    """Create a Sandbox instance using SDK, backed by ASGI transport."""
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60)
    r = await client.post("/sandboxes", json={"user_id": "sdk-test"})
    r.raise_for_status()
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)
    sb = Sandbox(sid, client=client)
    yield sb
    try:
        await sb.destroy()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_sdk_exec(sdk_sandbox):
    result = await sdk_sandbox.exec("echo hello-sdk")
    assert result["exit_code"] == 0
    assert "hello-sdk" in result["stdout"]


@pytest.mark.asyncio
async def test_sdk_files(sdk_sandbox):
    await sdk_sandbox.files.write("/workspace/test.txt", "sdk content")
    content = await sdk_sandbox.files.read("/workspace/test.txt")
    assert content == "sdk content"

    entries = await sdk_sandbox.files.list("/workspace")
    names = [e["name"] for e in entries]
    assert "test.txt" in names


@pytest.mark.asyncio
async def test_sdk_pause_resume(sdk_sandbox):
    info = await sdk_sandbox.pause()
    assert info["status"] == "paused"

    info = await sdk_sandbox.resume()
    assert info["status"] == "running"

    # Wait for daemon to recover after resume
    await _wait_for_daemon(sdk_sandbox.sandbox_id)

    # Exec should work after resume
    result = await sdk_sandbox.exec("echo after-resume")
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_sdk_status(sdk_sandbox):
    info = await sdk_sandbox.status()
    assert info["sandbox_id"] == sdk_sandbox.sandbox_id
    assert info["status"] == "running"
