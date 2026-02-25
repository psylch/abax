"""Browser automation proxy -- talks to sandbox_server.py daemon inside containers.

The daemon runs on port 8331 inside the container. Browser is lazily
initialized on first browser endpoint hit. Communication goes via
docker exec curl (works on macOS Docker Desktop and Linux).
"""

import asyncio

from infra.core.daemon import request_sync


async def navigate(sandbox_id: str, url: str) -> dict:
    return await asyncio.to_thread(
        request_sync, sandbox_id, "POST", "/navigate", {"url": url}
    )


async def screenshot(sandbox_id: str, full_page: bool = False) -> dict:
    return await asyncio.to_thread(
        request_sync, sandbox_id, "POST", "/screenshot", {"full_page": full_page}
    )


async def click(sandbox_id: str, selector: str) -> dict:
    return await asyncio.to_thread(
        request_sync, sandbox_id, "POST", "/click", {"selector": selector}
    )


async def type_text(sandbox_id: str, selector: str, text: str) -> dict:
    return await asyncio.to_thread(
        request_sync, sandbox_id, "POST", "/type", {"selector": selector, "text": text}
    )


async def get_content(sandbox_id: str, mode: str = "text") -> dict:
    return await asyncio.to_thread(
        request_sync, sandbox_id, "GET", f"/content?mode={mode}"
    )
