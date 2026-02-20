"""Shared fixtures and pre-flight checks for all tests."""
import subprocess

import docker
import pytest
from httpx import AsyncClient, ASGITransport
from gateway.main import app

SANDBOX_IMAGE = "abax-sandbox"


def pytest_configure(config):
    """Pre-flight: verify Docker is running and sandbox image exists."""
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.exit("Docker daemon is not running. Start Docker first.", returncode=1)

    try:
        c.images.get(SANDBOX_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"Image '{SANDBOX_IMAGE}' not found, building...")
        result = subprocess.run(
            ["docker", "build", "-t", SANDBOX_IMAGE, "./sandbox-image"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.exit(f"Failed to build sandbox image:\n{result.stderr}", returncode=1)
        print("Image built successfully.")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def sandbox_id(client):
    """Create a sandbox, yield its ID, cleanup after."""
    r = await client.post("/sandboxes", json={"user_id": "test-user"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    yield sid
    await client.delete(f"/sandboxes/{sid}")
