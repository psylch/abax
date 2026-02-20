"""Prometheus metrics for Abax gateway."""

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

# --- Sandbox metrics ---
sandboxes_active = Gauge(
    "abax_sandboxes_active",
    "Current number of active sandboxes",
)

sandbox_create_total = Counter(
    "abax_sandbox_create_total",
    "Total sandboxes created",
)

# --- Exec metrics ---
exec_duration_seconds = Histogram(
    "abax_exec_duration_seconds",
    "Command execution duration in seconds",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- GC metrics ---
gc_removed_total = Counter(
    "abax_gc_removed_total",
    "Total containers removed by GC",
)

# --- HTTP request metrics ---
requests_total = Counter(
    "abax_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)


def metrics_response_bytes() -> tuple[bytes, str]:
    """Return (body_bytes, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
