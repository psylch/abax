"""SDK file operations."""

import httpx


class FilesAPI:
    """File operations on a sandbox."""

    def __init__(self, sandbox_id: str, *, client: httpx.AsyncClient):
        self._sid = sandbox_id
        self._client = client

    async def read(self, path: str) -> str:
        """Read a text file from the sandbox."""
        r = await self._client.get(f"/sandboxes/{self._sid}/files/{path.lstrip('/')}")
        r.raise_for_status()
        return r.json()["content"]

    async def write(self, path: str, content: str) -> None:
        """Write a text file to the sandbox."""
        r = await self._client.put(
            f"/sandboxes/{self._sid}/files/{path.lstrip('/')}",
            json={"content": content, "path": path},
        )
        r.raise_for_status()

    async def list(self, path: str = "/workspace") -> list[dict]:
        """List directory contents. Returns list of {name, is_dir, size}."""
        r = await self._client.get(f"/sandboxes/{self._sid}/ls/{path.lstrip('/')}")
        r.raise_for_status()
        return r.json()["entries"]

    async def download_url(self, path: str) -> str:
        """Get a signed download URL for a file."""
        r = await self._client.get(
            f"/sandboxes/{self._sid}/files-url/{path.lstrip('/')}"
        )
        r.raise_for_status()
        return r.json()["url"]
