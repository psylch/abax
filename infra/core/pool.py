"""Warm pool — pre-creates sandbox containers for fast allocation.

Pool containers run idle with label ``abax.pool=true``. When a user requests
a sandbox, the pool container is removed and a fresh managed container is
created.  Because the Docker image layers are already cached, creation is
much faster than a cold start.
"""

import asyncio
import logging
import os

from infra.core.sandbox import LABEL_PREFIX, RUNTIME, SANDBOX_IMAGE, client

POOL_SIZE = int(os.getenv("ABAX_POOL_SIZE", "2"))
POOL_INTERVAL = 30  # seconds between pool replenishment checks
POOL_FILTER = {"label": f"{LABEL_PREFIX}.pool=true", "status": "running"}

logger = logging.getLogger("abax.pool")


def _create_pool_container_sync() -> str:
    """Create a warm pool container. Returns the short container ID."""
    run_kwargs = dict(
        image=SANDBOX_IMAGE,
        detach=True,
        labels={f"{LABEL_PREFIX}.pool": "true"},
        mem_limit="512m",
        cpu_quota=50000,
        cpu_period=100000,
    )
    if RUNTIME:
        run_kwargs["runtime"] = RUNTIME

    container = client.containers.run(**run_kwargs)
    cid = container.id[:12]
    logger.info("Pool: created warm container %s", cid)
    return cid


def _drain_one_sync() -> bool:
    """Remove one pool container to make room. Returns True if one was removed."""
    pool_containers = client.containers.list(filters=POOL_FILTER)
    if not pool_containers:
        return False
    pool_containers[0].remove(force=True)
    return True


async def drain_one() -> bool:
    return await asyncio.to_thread(_drain_one_sync)


def _pool_count_sync() -> int:
    return len(client.containers.list(filters=POOL_FILTER))


async def warm_pool_count() -> int:
    return await asyncio.to_thread(_pool_count_sync)


def _ensure_pool_sync() -> int:
    """Top up the warm pool to POOL_SIZE. Returns number of containers created."""
    current = _pool_count_sync()
    needed = POOL_SIZE - current
    created = 0
    for _ in range(needed):
        try:
            _create_pool_container_sync()
            created += 1
        except Exception:
            logger.exception("Pool: failed to create warm container")
            break
    return created


async def ensure_pool() -> int:
    return await asyncio.to_thread(_ensure_pool_sync)


async def pool_loop():
    """Background task that periodically replenishes the warm pool."""
    logger.info("Pool loop started (target_size=%d, interval=%ds)", POOL_SIZE, POOL_INTERVAL)
    try:
        created = await ensure_pool()
        if created:
            logger.info("Pool: initial fill created %d containers", created)
    except Exception:
        logger.exception("Pool: initial fill error")

    while True:
        await asyncio.sleep(POOL_INTERVAL)
        try:
            created = await ensure_pool()
            if created:
                logger.info("Pool: replenished %d containers", created)
        except Exception:
            logger.exception("Pool: replenishment error")
