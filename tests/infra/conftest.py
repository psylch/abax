"""Shared fixtures and pre-flight checks for all tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncio
import subprocess
import time

import docker
import pytest
from httpx import AsyncClient, ASGITransport
from infra.api.main import app

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


async def _wait_for_daemon(sandbox_id: str, timeout: float = 30):
    """Wait for the daemon inside a container to become healthy."""
    from infra.core.sandbox import get_container
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            container = get_container(sandbox_id)
            exit_code, _ = container.exec_run(
                ["curl", "-sf", "http://localhost:8331/health"],
                demux=True,
            )
            if exit_code == 0:
                return
        except Exception:
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError(f"Daemon in sandbox {sandbox_id} did not start within {timeout}s")


@pytest.fixture
async def sandbox_id(client):
    """Create a sandbox, wait for daemon, yield its ID, cleanup after."""
    r = await client.post("/sandboxes", json={"user_id": "test-user"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)
    yield sid
    await client.delete(f"/sandboxes/{sid}")
