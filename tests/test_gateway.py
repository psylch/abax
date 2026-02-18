"""
Integration tests for Abax Gateway.

Requires Docker daemon running and abax-sandbox image built.
Run: pytest tests/ -v
"""

import json
import pytest
from httpx import AsyncClient, ASGITransport
from gateway.main import app

transport = ASGITransport(app=app)


@pytest.fixture
async def client():
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def sandbox(client):
    """Create a sandbox and clean up after test."""
    r = await client.post("/sandboxes", json={"user_id": "test-user"})
    assert r.status_code == 200
    info = r.json()
    yield info
    # Cleanup
    await client.delete(f"/sandboxes/{info['sandbox_id']}")


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_create_and_list_sandbox(client):
    r = await client.post("/sandboxes", json={"user_id": "test-list"})
    assert r.status_code == 200
    info = r.json()
    assert info["user_id"] == "test-list"
    assert info["status"] == "running"

    r = await client.get("/sandboxes")
    ids = [s["sandbox_id"] for s in r.json()]
    assert info["sandbox_id"] in ids

    # Cleanup
    await client.delete(f"/sandboxes/{info['sandbox_id']}")


@pytest.mark.asyncio
async def test_exec_command(client, sandbox):
    sid = sandbox["sandbox_id"]
    r = await client.post(
        f"/sandboxes/{sid}/exec",
        json={"command": "echo hello"},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_exec_python(client, sandbox):
    sid = sandbox["sandbox_id"]
    r = await client.post(
        f"/sandboxes/{sid}/exec",
        json={"command": "python3 -c 'print(1+1)'"},
    )
    result = r.json()
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "2"


@pytest.mark.asyncio
async def test_exec_beancount(client, sandbox):
    sid = sandbox["sandbox_id"]
    # Write a minimal beancount file
    ledger = 'option "title" "Test"\n2026-01-01 open Assets:Cash CNY\n'
    await client.put(
        f"/sandboxes/{sid}/files/data/test.beancount",
        json={"content": ledger, "path": "/data/test.beancount"},
    )

    r = await client.post(
        f"/sandboxes/{sid}/exec",
        json={"command": "bean-check /data/test.beancount"},
    )
    result = r.json()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_file_read_write(client, sandbox):
    sid = sandbox["sandbox_id"]
    content = "hello from abax"
    await client.put(
        f"/sandboxes/{sid}/files/data/test.txt",
        json={"content": content, "path": "/data/test.txt"},
    )

    r = await client.get(f"/sandboxes/{sid}/files/data/test.txt")
    assert r.status_code == 200
    assert r.json()["content"] == content


@pytest.mark.asyncio
async def test_file_not_found(client, sandbox):
    sid = sandbox["sandbox_id"]
    r = await client.get(f"/sandboxes/{sid}/files/data/nonexistent.txt")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_sandbox_not_found(client):
    r = await client.get("/sandboxes/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_stop_and_destroy(client):
    r = await client.post("/sandboxes", json={"user_id": "test-stop"})
    info = r.json()
    sid = info["sandbox_id"]

    r = await client.post(f"/sandboxes/{sid}/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "exited"

    r = await client.delete(f"/sandboxes/{sid}")
    assert r.status_code == 204
