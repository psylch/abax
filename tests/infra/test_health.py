"""Test enhanced health check."""
import pytest
from infra.models import HealthResponse


def test_health_response_model():
    r = HealthResponse(
        status="ok",
        docker_connected=True,
        sandbox_image_ready=True,
        active_sandboxes=0,
    )
    assert r.status == "ok"
    assert r.docker_connected is True


def test_health_response_degraded():
    r = HealthResponse(
        status="degraded",
        docker_connected=True,
        sandbox_image_ready=False,
        active_sandboxes=0,
    )
    assert r.status == "degraded"
