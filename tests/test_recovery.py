"""Test crash recovery — reconcile Docker state with SQLite store."""
import docker
import pytest

from gateway.recovery import recover_state
from gateway.sandbox import LABEL_PREFIX, SANDBOX_IMAGE, client
from gateway.store import store


def _create_labeled_container(user_id: str = "test-recovery") -> str:
    """Create a Docker container with abax labels (simulates a pre-crash container)."""
    container = client.containers.run(
        SANDBOX_IMAGE,
        detach=True,
        labels={
            f"{LABEL_PREFIX}.managed": "true",
            f"{LABEL_PREFIX}.user_id": user_id,
        },
    )
    return container.id[:12]


def _create_pool_container() -> str:
    """Create a pool container (simulates a stale pool leftover from crash)."""
    container = client.containers.run(
        SANDBOX_IMAGE,
        detach=True,
        labels={f"{LABEL_PREFIX}.pool": "true"},
    )
    return container.id[:12]


def _container_exists(cid: str) -> bool:
    try:
        client.containers.get(cid)
        return True
    except docker.errors.NotFound:
        return False


def _cleanup_container(cid: str):
    try:
        client.containers.get(cid).remove(force=True)
    except docker.errors.NotFound:
        pass


@pytest.mark.asyncio
async def test_recovery_registers_orphan_docker_container():
    """Container in Docker but not in store should be re-registered."""
    cid = _create_labeled_container("user-orphan")
    try:
        # Ensure it's NOT in the store (simulates crash losing store state)
        store.unregister(cid)
        assert store.get_sandbox_meta(cid) is None

        summary = await recover_state()

        # Container should now be registered in store
        meta = store.get_sandbox_meta(cid)
        assert meta is not None
        assert meta["user_id"] == "user-orphan"
        assert summary["recovered"] >= 1
    finally:
        _cleanup_container(cid)
        store.unregister(cid)


@pytest.mark.asyncio
async def test_recovery_removes_stale_store_entry():
    """Store entry with no Docker container should be removed."""
    fake_id = "deadbeef1234"
    store.register(fake_id, "ghost-user")
    assert store.get_sandbox_meta(fake_id) is not None

    summary = await recover_state()

    assert store.get_sandbox_meta(fake_id) is None
    assert summary["unregistered"] >= 1


@pytest.mark.asyncio
async def test_recovery_cleans_stale_pool_containers():
    """Pool containers from previous crash should be removed."""
    cid = _create_pool_container()
    try:
        assert _container_exists(cid)

        summary = await recover_state()

        assert not _container_exists(cid)
        assert summary["pool_cleaned"] >= 1
    finally:
        _cleanup_container(cid)


@pytest.mark.asyncio
async def test_recovery_no_action_when_consistent():
    """When Docker and store are in sync, recovery takes no action."""
    # Create a container AND register it in store (consistent state)
    cid = _create_labeled_container("user-consistent")
    store.register(cid, "user-consistent")
    try:
        summary = await recover_state()

        # The container was already registered, so no recovery needed for it
        meta = store.get_sandbox_meta(cid)
        assert meta is not None
        assert meta["user_id"] == "user-consistent"
    finally:
        _cleanup_container(cid)
        store.unregister(cid)
