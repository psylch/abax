"""Tests for the SSE event push system."""
import asyncio
import json

import httpx
import pytest


@pytest.mark.asyncio
async def test_event_bus_subscribe_publish():
    """Unit test: EventBus pub/sub works correctly."""
    from gateway.events import EventBus

    bus = EventBus()
    q = bus.subscribe("sb-1")

    await bus.publish("sb-1", "sandbox.created", {"user_id": "u1"})

    event = q.get_nowait()
    assert event["type"] == "sandbox.created"
    assert event["sandbox_id"] == "sb-1"
    assert event["data"]["user_id"] == "u1"
    assert "timestamp" in event

    bus.unsubscribe("sb-1", q)


@pytest.mark.asyncio
async def test_event_bus_multiple_subscribers():
    """Multiple subscribers each receive the same event."""
    from gateway.events import EventBus

    bus = EventBus()
    q1 = bus.subscribe("sb-2")
    q2 = bus.subscribe("sb-2")

    await bus.publish("sb-2", "exec.started", {"command": "ls"})

    e1 = q1.get_nowait()
    e2 = q2.get_nowait()
    assert e1["type"] == "exec.started"
    assert e2["type"] == "exec.started"

    bus.unsubscribe("sb-2", q1)
    bus.unsubscribe("sb-2", q2)


@pytest.mark.asyncio
async def test_event_bus_isolation():
    """Events for one sandbox don't leak to another."""
    from gateway.events import EventBus

    bus = EventBus()
    q_a = bus.subscribe("sb-a")
    q_b = bus.subscribe("sb-b")

    await bus.publish("sb-a", "sandbox.stopped")

    assert q_b.empty()
    assert q_a.qsize() == 1

    bus.unsubscribe("sb-a", q_a)
    bus.unsubscribe("sb-b", q_b)


@pytest.mark.asyncio
async def test_sse_stream_format():
    """SSE stream yields correctly formatted event strings."""
    from gateway.events import sse_stream, bus as global_bus

    async def collect_one():
        async for chunk in sse_stream("sb-sse"):
            return chunk

    # Publish after a short delay so the stream has time to await
    async def delayed_publish():
        await asyncio.sleep(0.05)
        await global_bus.publish("sb-sse", "file.written", {"path": "/test.txt"})

    task = asyncio.create_task(delayed_publish())
    chunk = await asyncio.wait_for(collect_one(), timeout=2.0)
    await task

    assert chunk.startswith("event: file.written\n")
    assert "data: " in chunk
    assert chunk.endswith("\n\n")

    data_line = chunk.split("\n")[1]
    payload = json.loads(data_line[len("data: "):])
    assert payload["type"] == "file.written"
    assert payload["data"]["path"] == "/test.txt"


@pytest.mark.asyncio
async def test_sse_route_exists(client):
    """The SSE route is registered and returns text/event-stream."""
    # The stream will hang waiting for events, so we use a short timeout
    try:
        r = await asyncio.wait_for(
            client.get("/sandboxes/fake-sb/events"),
            timeout=0.5,
        )
        # If we get a response, check content type
        assert "text/event-stream" in r.headers.get("content-type", "")
    except (asyncio.TimeoutError, httpx.ReadTimeout):
        # Expected — the SSE stream blocks waiting for events
        pass


@pytest.mark.asyncio
async def test_create_sandbox_emits_event(client):
    """Creating a sandbox publishes a sandbox.created event."""
    from gateway.events import bus
    from tests.conftest import _wait_for_daemon

    # Subscribe before creating
    # We don't know the sandbox_id yet, so we'll check after
    r = await client.post("/sandboxes", json={"user_id": "test-events"})
    assert r.status_code == 200
    sid = r.json()["sandbox_id"]
    await _wait_for_daemon(sid)

    # For the next operation, subscribe first, then act
    q = bus.subscribe(sid)

    # Exec a command — should emit exec.started + exec.completed
    r = await client.post(
        f"/sandboxes/{sid}/exec",
        json={"command": "echo hi"},
    )
    assert r.status_code == 200

    events = []
    while not q.empty():
        events.append(q.get_nowait())

    event_types = [e["type"] for e in events]
    assert "exec.started" in event_types
    assert "exec.completed" in event_types

    bus.unsubscribe(sid, q)
    await client.delete(f"/sandboxes/{sid}")


@pytest.mark.asyncio
async def test_file_write_emits_event(client, sandbox_id):
    """Writing a file publishes a file.written event."""
    from gateway.events import bus

    q = bus.subscribe(sandbox_id)

    await client.put(
        f"/sandboxes/{sandbox_id}/files/tmp/test.txt",
        json={"content": "hello", "path": "/tmp/test.txt"},
    )

    events = []
    while not q.empty():
        events.append(q.get_nowait())

    event_types = [e["type"] for e in events]
    assert "file.written" in event_types

    written_event = next(e for e in events if e["type"] == "file.written")
    assert written_event["data"]["path"] == "/tmp/test.txt"

    bus.unsubscribe(sandbox_id, q)


@pytest.mark.asyncio
async def test_unsubscribe_cleanup():
    """After unsubscribe, events are no longer received."""
    from gateway.events import EventBus

    bus = EventBus()
    q = bus.subscribe("sb-cleanup")
    bus.unsubscribe("sb-cleanup", q)

    await bus.publish("sb-cleanup", "sandbox.stopped")
    assert q.empty()
