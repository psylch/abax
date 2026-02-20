"""SSE event bus for real-time sandbox event notifications."""
import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import AsyncGenerator

logger = logging.getLogger("abax.events")


class EventBus:
    """Per-sandbox pub/sub event bus using asyncio.Queue per subscriber."""

    def __init__(self):
        # sandbox_id -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, sandbox_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[sandbox_id].append(q)
        return q

    def unsubscribe(self, sandbox_id: str, q: asyncio.Queue):
        subs = self._subscribers.get(sandbox_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            self._subscribers.pop(sandbox_id, None)

    async def publish(self, sandbox_id: str, event_type: str, data: dict | None = None):
        event = {
            "sandbox_id": sandbox_id,
            "type": event_type,
            "data": data or {},
            "timestamp": time.time(),
        }
        for q in self._subscribers.get(sandbox_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping event for full subscriber queue: %s", event_type)


# Module-level singleton
bus = EventBus()


async def publish(sandbox_id: str, event_type: str, data: dict | None = None):
    """Convenience wrapper around the global bus."""
    await bus.publish(sandbox_id, event_type, data)


async def sse_stream(sandbox_id: str) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events for a sandbox. Use as StreamingResponse body."""
    q = bus.subscribe(sandbox_id)
    try:
        while True:
            event = await q.get()
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        bus.unsubscribe(sandbox_id, q)
