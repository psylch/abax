"""Tests for API Key authentication dependency."""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.auth import verify_api_key

# ---------- Test app ----------

test_app = FastAPI()


@test_app.get("/protected")
async def protected(_=Depends(verify_api_key)):
    return {"ok": True}


@test_app.get("/public")
async def public():
    return {"ok": True}


@pytest.fixture
def client():
    transport = ASGITransport(app=test_app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------- Dev mode (ABAX_API_KEY not set) ----------


async def test_dev_mode_no_key_set(client: AsyncClient, monkeypatch):
    """When ABAX_API_KEY is unset, all requests pass without auth."""
    monkeypatch.delenv("ABAX_API_KEY", raising=False)
    resp = await client.get("/protected")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_dev_mode_public_endpoint(client: AsyncClient, monkeypatch):
    monkeypatch.delenv("ABAX_API_KEY", raising=False)
    resp = await client.get("/public")
    assert resp.status_code == 200


# ---------- Auth enabled (ABAX_API_KEY is set) ----------

API_KEY = "test-secret-key-12345"


async def test_no_token_returns_401(client: AsyncClient, monkeypatch):
    monkeypatch.setenv("ABAX_API_KEY", API_KEY)
    resp = await client.get("/protected")
    assert resp.status_code == 401


async def test_wrong_token_returns_401(client: AsyncClient, monkeypatch):
    monkeypatch.setenv("ABAX_API_KEY", API_KEY)
    resp = await client.get("/protected", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401


async def test_correct_token_returns_200(client: AsyncClient, monkeypatch):
    monkeypatch.setenv("ABAX_API_KEY", API_KEY)
    resp = await client.get(
        "/protected", headers={"Authorization": f"Bearer {API_KEY}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_public_endpoint_always_accessible(client: AsyncClient, monkeypatch):
    """Public endpoints (no verify_api_key dep) are always accessible."""
    monkeypatch.setenv("ABAX_API_KEY", API_KEY)
    resp = await client.get("/public")
    assert resp.status_code == 200


# ---------- TODO: /health exemption ----------
# The /health endpoint in the real app should be exempt from auth.
# This needs to be handled during main.py integration (do NOT add
# verify_api_key as a dependency on /health).
