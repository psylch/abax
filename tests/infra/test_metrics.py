"""Tests for Prometheus metrics and structured logging."""

import json
import logging

import pytest

from infra.metrics import (
    sandboxes_active,
    sandbox_create_total,
    exec_duration_seconds,
    gc_removed_total,
    requests_total,
)
from infra.logging_config import (
    JSONFormatter,
    request_id_var,
    sandbox_id_var,
)


@pytest.fixture(autouse=True)
def reset_metrics():
    """Reset labeled counter values between tests to avoid cross-test pollution."""
    yield
    requests_total._metrics.clear()


# --- /metrics endpoint tests ---


async def test_metrics_endpoint_returns_200(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


async def test_metrics_endpoint_contains_expected_metrics(client):
    r = await client.get("/metrics")
    body = r.text
    assert "abax_sandboxes_active" in body
    assert "abax_sandbox_create_total" in body
    assert "abax_exec_duration_seconds" in body
    assert "abax_gc_removed_total" in body
    assert "abax_requests_total" in body


async def test_metrics_no_auth_required(client):
    """The /metrics endpoint should work without any API key."""
    r = await client.get("/metrics")
    assert r.status_code == 200


# --- Request middleware tests ---


async def test_request_id_header_returned(client):
    r = await client.get("/health")
    assert "X-Request-ID" in r.headers
    assert len(r.headers["X-Request-ID"]) > 0


async def test_custom_request_id_echoed(client):
    r = await client.get("/health", headers={"X-Request-ID": "test-req-123"})
    assert r.headers["X-Request-ID"] == "test-req-123"


async def test_requests_total_incremented(client):
    """After hitting /health, the requests_total counter should have a sample."""
    await client.get("/health")
    r = await client.get("/metrics")
    body = r.text
    # Should see a counter line for GET /health 200
    assert 'abax_requests_total{method="GET",path="/health",status="200"}' in body


# --- Metric helper tests ---


def test_gauge_inc_dec():
    val_before = sandboxes_active._value.get()
    sandboxes_active.inc()
    assert sandboxes_active._value.get() == val_before + 1
    sandboxes_active.dec()
    assert sandboxes_active._value.get() == val_before


def test_counter_inc():
    val_before = sandbox_create_total._value.get()
    sandbox_create_total.inc()
    assert sandbox_create_total._value.get() == val_before + 1


def test_histogram_observe():
    exec_duration_seconds.observe(0.5)
    # Just verify no exception; histogram sum should increase


def test_gc_counter():
    val_before = gc_removed_total._value.get()
    gc_removed_total.inc(3)
    assert gc_removed_total._value.get() == val_before + 3


# --- Structured logging tests ---


def test_json_formatter_basic():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "test"
    assert "timestamp" in data


def test_json_formatter_with_request_id():
    formatter = JSONFormatter()
    token = request_id_var.set("req-abc")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test msg",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["request_id"] == "req-abc"
    finally:
        request_id_var.reset(token)


def test_json_formatter_with_sandbox_id():
    formatter = JSONFormatter()
    token = sandbox_id_var.set("sb-xyz")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test msg",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["sandbox_id"] == "sb-xyz"
    finally:
        sandbox_id_var.reset(token)


def test_json_formatter_no_context_vars():
    formatter = JSONFormatter()
    # Ensure context vars are not set
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="bare message",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)
    assert "request_id" not in data
    assert "sandbox_id" not in data
