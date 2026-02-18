import os
from pathlib import Path

import docker
from docker.errors import NotFound

from gateway.models import SandboxInfo

SANDBOX_IMAGE = os.getenv("ABAX_SANDBOX_IMAGE", "abax-sandbox")
PERSISTENT_ROOT = Path(os.getenv("ABAX_PERSISTENT_ROOT", "/tmp/abax-persistent"))
LABEL_PREFIX = "abax"

client = docker.from_env()


def _container_to_info(container) -> SandboxInfo:
    container.reload()
    return SandboxInfo(
        sandbox_id=container.id[:12],
        user_id=container.labels.get(f"{LABEL_PREFIX}.user_id", ""),
        status=container.status,
    )


def create_sandbox(user_id: str) -> SandboxInfo:
    user_data = PERSISTENT_ROOT / user_id
    user_data.mkdir(parents=True, exist_ok=True)

    container = client.containers.run(
        SANDBOX_IMAGE,
        detach=True,
        labels={
            f"{LABEL_PREFIX}.user_id": user_id,
            f"{LABEL_PREFIX}.managed": "true",
        },
        volumes={
            str(user_data): {"bind": "/data", "mode": "rw"},
        },
        mem_limit="512m",
        cpu_quota=50000,  # 0.5 CPU (50% of one core)
        cpu_period=100000,
    )
    return _container_to_info(container)


def get_sandbox(sandbox_id: str) -> SandboxInfo:
    container = client.containers.get(sandbox_id)
    return _container_to_info(container)


def list_sandboxes() -> list[SandboxInfo]:
    containers = client.containers.list(
        all=True,
        filters={"label": f"{LABEL_PREFIX}.managed=true"},
    )
    return [_container_to_info(c) for c in containers]


def stop_sandbox(sandbox_id: str) -> SandboxInfo:
    container = client.containers.get(sandbox_id)
    container.stop(timeout=5)
    return _container_to_info(container)


def destroy_sandbox(sandbox_id: str) -> None:
    container = client.containers.get(sandbox_id)
    container.remove(force=True)


def get_container(sandbox_id: str):
    """Get the raw docker container object for exec operations."""
    return client.containers.get(sandbox_id)
