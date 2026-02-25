"""Crash recovery — reconcile Docker containers with the SQLite store on startup.

After a gateway crash or restart, Docker containers may still be running but
the store may be stale or empty (e.g. if using an ephemeral /tmp database).
This module scans Docker for abax-managed containers and reconciles state.
"""

import asyncio
import logging

import docker

from infra.core.sandbox import LABEL_PREFIX, client
from infra.core.store import store

logger = logging.getLogger("abax.recovery")


def _recover_state_sync() -> dict:
    """Reconcile Docker containers with the SQLite store.

    Returns a summary dict with counts of actions taken.
    """
    summary = {"recovered": 0, "unregistered": 0, "pool_cleaned": 0}

    # --- Phase 1: Recover orphan Docker containers (in Docker but not in store) ---
    managed_containers = client.containers.list(
        all=True,
        filters={"label": f"{LABEL_PREFIX}.managed=true"},
    )

    store_ids = set(store.all_sandbox_ids())
    docker_ids = {c.id[:12]: c for c in managed_containers}

    # Containers in Docker but missing from store — re-register them
    for cid, container in docker_ids.items():
        if cid not in store_ids:
            user_id = container.labels.get(f"{LABEL_PREFIX}.user_id", "unknown")
            logger.info(
                "Recovery: re-registering orphan container %s (user=%s, status=%s)",
                cid,
                user_id,
                container.status,
            )
            store.register(cid, user_id)
            summary["recovered"] += 1

    # --- Phase 2: Clean orphan store entries (in store but not in Docker) ---
    for sid in store_ids:
        if sid not in docker_ids:
            logger.info("Recovery: removing stale store entry %s (no Docker container)", sid)
            store.unregister(sid)
            summary["unregistered"] += 1

    # --- Phase 3: Clean up stale pool containers from previous crash ---
    try:
        pool_containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.pool=true"},
        )
        for container in pool_containers:
            cid = container.id[:12]
            logger.info("Recovery: removing stale pool container %s", cid)
            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
            summary["pool_cleaned"] += 1
    except Exception:
        logger.exception("Recovery: error cleaning pool containers")

    return summary


async def recover_state() -> dict:
    """Async wrapper for crash recovery. Call during lifespan startup."""
    summary = await asyncio.to_thread(_recover_state_sync)

    total = summary["recovered"] + summary["unregistered"] + summary["pool_cleaned"]
    if total > 0:
        logger.info(
            "Recovery complete: %d containers re-registered, %d stale entries removed, %d pool containers cleaned",
            summary["recovered"],
            summary["unregistered"],
            summary["pool_cleaned"],
        )
    else:
        logger.info("Recovery: no action needed, state is consistent")

    return summary
