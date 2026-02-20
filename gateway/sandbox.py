import asyncio
import os
import threading
from pathlib import Path

import docker
from docker.errors import NotFound

from gateway.models import SandboxInfo

SANDBOX_IMAGE = os.getenv("ABAX_SANDBOX_IMAGE", "abax-sandbox")
PERSISTENT_ROOT = Path(os.getenv("ABAX_PERSISTENT_ROOT", "/tmp/abax-persistent"))
LABEL_PREFIX = "abax"
MAX_SANDBOXES = int(os.getenv("ABAX_MAX_SANDBOXES", "10"))
MAX_SANDBOXES_PER_USER = int(os.getenv("ABAX_MAX_SANDBOXES_PER_USER", "3"))
RUNTIME = os.getenv("ABAX_RUNTIME", "")  # empty = Docker default (runc), "runsc" for gVisor

client = docker.from_env()

# Lock to serialize create operations — prevents TOCTOU race on limit checks
_create_lock = threading.Lock()


class SandboxLimitExceeded(Exception):
    pass


def _container_to_info(container) -> SandboxInfo:
    container.reload()
    return SandboxInfo(
        sandbox_id=container.id[:12],
        user_id=container.labels.get(f"{LABEL_PREFIX}.user_id", ""),
        status=container.status,
    )


def _create_sandbox_sync(user_id: str) -> SandboxInfo:
    with _create_lock:
        # Check global sandbox limit
        all_containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.managed=true"},
        )
        if len(all_containers) >= MAX_SANDBOXES:
            raise SandboxLimitExceeded(f"Maximum {MAX_SANDBOXES} sandboxes reached")

        # Check per-user sandbox limit
        user_containers = [
            c for c in all_containers
            if c.labels.get(f"{LABEL_PREFIX}.user_id") == user_id
        ]
        if len(user_containers) >= MAX_SANDBOXES_PER_USER:
            raise SandboxLimitExceeded(
                f"User {user_id} has reached the maximum of {MAX_SANDBOXES_PER_USER} sandboxes"
            )

        user_data = (PERSISTENT_ROOT / user_id).resolve()
        if not str(user_data).startswith(str(PERSISTENT_ROOT.resolve())):
            raise SandboxLimitExceeded(f"Invalid user_id: {user_id}")
        user_data.mkdir(parents=True, exist_ok=True)

        run_kwargs = dict(
            image=SANDBOX_IMAGE,
            detach=True,
            labels={
                f"{LABEL_PREFIX}.user_id": user_id,
                f"{LABEL_PREFIX}.managed": "true",
            },
            volumes={
                str(user_data): {"bind": "/data", "mode": "rw"},
            },
            mem_limit="512m",
            cpu_quota=50000,
            cpu_period=100000,
        )
        if RUNTIME:
            run_kwargs["runtime"] = RUNTIME

        container = client.containers.run(**run_kwargs)
        return _container_to_info(container)


async def create_sandbox(user_id: str) -> SandboxInfo:
    return await asyncio.to_thread(_create_sandbox_sync, user_id)


async def get_sandbox(sandbox_id: str) -> SandboxInfo:
    return await asyncio.to_thread(
        lambda: _container_to_info(client.containers.get(sandbox_id))
    )


async def list_sandboxes() -> list[SandboxInfo]:
    def _list():
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.managed=true"},
        )
        return [_container_to_info(c) for c in containers]
    return await asyncio.to_thread(_list)


async def stop_sandbox(sandbox_id: str) -> SandboxInfo:
    def _stop():
        container = client.containers.get(sandbox_id)
        container.stop(timeout=5)
        return _container_to_info(container)
    return await asyncio.to_thread(_stop)


class SandboxStateError(Exception):
    pass


async def pause_sandbox(sandbox_id: str) -> SandboxInfo:
    """Pause a running sandbox (freezes all processes, preserves memory state)."""
    def _pause():
        container = client.containers.get(sandbox_id)
        container.reload()
        if container.status != "running":
            raise SandboxStateError(f"Cannot pause: sandbox is {container.status}")
        container.pause()
        return _container_to_info(container)
    return await asyncio.to_thread(_pause)


async def resume_sandbox(sandbox_id: str) -> SandboxInfo:
    """Resume a paused sandbox."""
    def _resume():
        container = client.containers.get(sandbox_id)
        container.reload()
        if container.status != "paused":
            raise SandboxStateError(f"Cannot resume: sandbox is {container.status}")
        container.unpause()
        return _container_to_info(container)
    return await asyncio.to_thread(_resume)


async def destroy_sandbox(sandbox_id: str) -> None:
    def _destroy():
        container = client.containers.get(sandbox_id)
        container.remove(force=True)
    await asyncio.to_thread(_destroy)


def get_container(sandbox_id: str):
    """Get the raw docker container object for exec operations."""
    return client.containers.get(sandbox_id)


async def get_container_ip(sandbox_id: str) -> str:
    """Get the container's internal IP address on the bridge network."""
    def _get_ip():
        container = client.containers.get(sandbox_id)
        container.reload()
        networks = container.attrs["NetworkSettings"]["Networks"]
        # Use the first available network (typically 'bridge')
        for net_name, net_info in networks.items():
            ip = net_info.get("IPAddress")
            if ip:
                return ip
        raise RuntimeError(f"No IP address found for container {sandbox_id}")
    return await asyncio.to_thread(_get_ip)
