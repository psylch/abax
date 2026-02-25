"""Tests for the SandboxStore metadata persistence layer."""
import os
import tempfile
import time

import pytest

from infra.core.store import SandboxStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test-metadata.db")
    return SandboxStore(db_path=db_path)


def test_register_and_get_meta(store):
    store.register("abc123", "user-1")
    meta = store.get_sandbox_meta("abc123")
    assert meta is not None
    assert meta["sandbox_id"] == "abc123"
    assert meta["user_id"] == "user-1"
    assert meta["created_at"] <= time.time()
    assert meta["last_active_at"] <= time.time()


def test_record_activity_updates_last_active(store):
    store.register("abc123", "user-1")
    meta_before = store.get_sandbox_meta("abc123")

    # Ensure a measurable time difference
    time.sleep(0.05)
    store.record_activity("abc123")

    meta_after = store.get_sandbox_meta("abc123")
    assert meta_after["last_active_at"] > meta_before["last_active_at"]
    # created_at should remain unchanged
    assert meta_after["created_at"] == meta_before["created_at"]


def test_get_idle_sandboxes(store):
    store.register("idle-1", "user-1")
    store.register("idle-2", "user-1")
    store.register("active-1", "user-1")

    # Make idle-1 and idle-2 look old
    conn = store._connect()
    old_time = time.time() - 3600
    conn.execute(
        "UPDATE sandboxes SET last_active_at = ? WHERE sandbox_id IN (?, ?)",
        (old_time, "idle-1", "idle-2"),
    )
    conn.commit()
    conn.close()

    idle = store.get_idle_sandboxes(max_idle_seconds=1800)
    assert "idle-1" in idle
    assert "idle-2" in idle
    assert "active-1" not in idle


def test_unregister_removes_record(store):
    store.register("to-remove", "user-1")
    assert store.get_sandbox_meta("to-remove") is not None

    store.unregister("to-remove")
    assert store.get_sandbox_meta("to-remove") is None


def test_get_meta_returns_none_for_unknown(store):
    assert store.get_sandbox_meta("nonexistent") is None


def test_all_sandbox_ids(store):
    store.register("a", "u1")
    store.register("b", "u2")
    store.register("c", "u3")
    ids = store.all_sandbox_ids()
    assert set(ids) == {"a", "b", "c"}
