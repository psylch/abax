"""SDK browser automation."""

import httpx


class BrowserAPI:
    """Browser automation on a sandbox."""

    def __init__(self, sandbox_id: str, *, client: httpx.AsyncClient):
        self._sid = sandbox_id
        self._client = client

    async def navigate(self, url: str) -> dict:
        """Navigate to a URL. Returns {title, url}."""
        r = await self._client.post(
            f"/sandboxes/{self._sid}/browser/navigate",
            json={"url": url},
        )
        r.raise_for_status()
        return r.json()

    async def screenshot(self, full_page: bool = False) -> dict:
        """Take a screenshot. Returns {data_b64, format}."""
        r = await self._client.post(
            f"/sandboxes/{self._sid}/browser/screenshot",
            json={"full_page": full_page},
        )
        r.raise_for_status()
        return r.json()

    async def click(self, selector: str) -> dict:
        """Click an element by CSS selector."""
        r = await self._client.post(
            f"/sandboxes/{self._sid}/browser/click",
            json={"selector": selector},
        )
        r.raise_for_status()
        return r.json()

    async def type(self, selector: str, text: str) -> dict:
        """Type text into an element by CSS selector."""
        r = await self._client.post(
            f"/sandboxes/{self._sid}/browser/type",
            json={"selector": selector, "text": text},
        )
        r.raise_for_status()
        return r.json()

    async def content(self, mode: str = "text") -> dict:
        """Get page content. Returns {content, url, title}."""
        r = await self._client.get(
            f"/sandboxes/{self._sid}/browser/content",
            params={"mode": mode},
        )
        r.raise_for_status()
        return r.json()
