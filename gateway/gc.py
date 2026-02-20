import asyncio
import logging
import os
import time

import docker

from gateway.store import store

LABEL_PREFIX = "abax"
GC_INTERVAL = int(os.getenv("ABAX_GC_INTERVAL", "60"))
MAX_IDLE_SECONDS = int(os.getenv("ABAX_MAX_IDLE", "1800"))
MAX_PAUSE_SECONDS = int(os.getenv("ABAX_MAX_PAUSE", "86400"))  # 24 hours

logger = logging.getLogger("abax.gc")
client = docker.from_env()


async def collect_garbage(
    max_idle_seconds: int = MAX_IDLE_SECONDS,
    max_pause_seconds: int = MAX_PAUSE_SECONDS,
) -> list[str]:
    """Remove exited abax containers, idle running containers, and long-paused containers.

    Returns list of removed container IDs (short 12-char form).
    """

    def _collect():
        removed = []

        # --- Phase 1: Sync store with Docker reality ---
        # Remove store entries whose containers no longer exist in Docker.
        for sid in store.all_sandbox_ids():
            try:
                client.containers.get(sid)
            except docker.errors.NotFound:
                logger.info("GC sync: container %s gone from Docker, removing from store", sid)
                store.unregister(sid)

        # --- Phase 2: Remove exited containers ---
        exited = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_PREFIX}.managed=true", "status": "exited"},
        )
        for container in exited:
            cid = container.id[:12]
            logger.info("GC removing exited container %s", cid)
            container.remove(force=True)
            store.unregister(cid)
            removed.append(cid)

        # --- Phase 3: Stop and remove idle running containers ---
        # Skip paused containers — they are handled separately in Phase 4.
        idle_ids = store.get_idle_sandboxes(max_idle_seconds)
        for sid in idle_ids:
            try:
                container = client.containers.get(sid)
                container.reload()
                if container.status == "paused":
                    # Paused containers are NOT stopped by idle GC.
                    # They will be handled in Phase 4 if paused too long.
                    continue
                if container.status == "running":
                    logger.info("GC stopping idle container %s", sid)
                    container.stop(timeout=5)
                container.remove(force=True)
                logger.info("GC removed idle container %s", sid)
            except docker.errors.NotFound:
                logger.info("GC idle container %s already gone", sid)
            store.unregister(sid)
            removed.append(sid)

        # --- Phase 4: Remove containers paused too long ---
        # Use created_at from store as a rough proxy for pause duration.
        if max_pause_seconds > 0:
            paused = client.containers.list(
                all=True,
                filters={"label": f"{LABEL_PREFIX}.managed=true", "status": "paused"},
            )
            now = time.time()
            for container in paused:
                cid = container.id[:12]
                meta = store.get_sandbox_meta(cid)
                if meta is None:
                    continue
                # Use last_active_at as proxy for when the container was paused.
                # This is approximate but sufficient — a paused container won't have
                # any new activity, so last_active_at reflects roughly when it was
                # last used (which is at or before the pause).
                pause_duration = now - meta["last_active_at"]
                if pause_duration >= max_pause_seconds:
                    logger.info(
                        "GC removing long-paused container %s (paused ~%.0fs)",
                        cid,
                        pause_duration,
                    )
                    try:
                        container.unpause()
                        container.stop(timeout=5)
                        container.remove(force=True)
                    except docker.errors.NotFound:
                        logger.info("GC long-paused container %s already gone", cid)
                    store.unregister(cid)
                    removed.append(cid)

        return removed

    return await asyncio.to_thread(_collect)


async def gc_loop():
    """Background loop that runs GC periodically."""
    logger.info("GC loop started (interval=%ds, max_idle=%ds)", GC_INTERVAL, MAX_IDLE_SECONDS)
    while True:
        await asyncio.sleep(GC_INTERVAL)
        try:
            removed = await collect_garbage()
            if removed:
                logger.info("GC removed %d containers: %s", len(removed), removed)
        except Exception:
            logger.exception("GC error")
