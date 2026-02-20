"""Abax Python SDK — high-level interface to the sandbox gateway.

Usage::

    from sdk import Sandbox

    async with Sandbox.create("user-1") as sb:
        result = await sb.exec("echo hello")
        print(result["stdout"])
"""

import httpx

from sdk.files import FilesAPI
from sdk.browser import BrowserAPI


class Sandbox:
    """A sandbox session backed by the Abax Gateway."""

    def __init__(self, sandbox_id: str, *, client: httpx.AsyncClient):
        self.sandbox_id = sandbox_id
        self._client = client
        self.files = FilesAPI(sandbox_id, client=client)
        self.browser = BrowserAPI(sandbox_id, client=client)

    # --- Factory methods ---

    @staticmethod
    def _make_client(
        base_url: str, api_key: str | None,
    ) -> httpx.AsyncClient:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60)

    @classmethod
    async def create(
        cls,
        user_id: str,
        *,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
    ) -> "Sandbox":
        """Create a new sandbox and return a connected Sandbox instance."""
        client = cls._make_client(base_url, api_key)
        try:
            r = await client.post("/sandboxes", json={"user_id": user_id})
            r.raise_for_status()
        except Exception:
            await client.aclose()
            raise
        return cls(r.json()["sandbox_id"], client=client)

    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        *,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
    ) -> "Sandbox":
        """Connect to an existing sandbox by ID."""
        client = cls._make_client(base_url, api_key)
        try:
            r = await client.get(f"/sandboxes/{sandbox_id}")
            r.raise_for_status()
        except Exception:
            await client.aclose()
            raise
        return cls(sandbox_id, client=client)

    # --- Lifecycle ---

    async def status(self) -> dict:
        r = await self._client.get(f"/sandboxes/{self.sandbox_id}")
        r.raise_for_status()
        return r.json()

    async def pause(self) -> dict:
        r = await self._client.post(f"/sandboxes/{self.sandbox_id}/pause")
        r.raise_for_status()
        return r.json()

    async def resume(self) -> dict:
        r = await self._client.post(f"/sandboxes/{self.sandbox_id}/resume")
        r.raise_for_status()
        return r.json()

    async def stop(self) -> dict:
        r = await self._client.post(f"/sandboxes/{self.sandbox_id}/stop")
        r.raise_for_status()
        return r.json()

    async def destroy(self) -> None:
        r = await self._client.delete(f"/sandboxes/{self.sandbox_id}")
        r.raise_for_status()

    # --- Exec ---

    async def exec(self, command: str, timeout: int = 30) -> dict:
        """Execute a command and return {stdout, stderr, exit_code, duration_ms}."""
        r = await self._client.post(
            f"/sandboxes/{self.sandbox_id}/exec",
            json={"command": command, "timeout": timeout},
        )
        r.raise_for_status()
        return r.json()

    # --- Context manager ---

    async def __aenter__(self) -> "Sandbox":
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()
