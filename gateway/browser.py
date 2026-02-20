"""Browser automation proxy — talks to browser_server.py inside sandbox containers.

The browser server (FastAPI + Playwright) runs on port 8330 inside the
container and is started lazily on the first browser request.  All
communication goes through ``docker exec`` + ``curl`` so no extra port
mapping is required on the host.
"""

import asyncio
import json
import time

from gateway.sandbox import get_container


HEALTH_CMD = ["curl", "-sf", "http://localhost:8330/health"]


def _ensure_browser_server(container) -> None:
    """Start the browser server inside the container if it is not already running."""
    exit_code, _ = container.exec_run(HEALTH_CMD, demux=True)
    if exit_code == 0:
        return

    container.exec_run(
        ["bash", "-c", "nohup python3 /opt/browser_server.py > /tmp/browser.log 2>&1 &"],
        detach=True,
    )

    # Poll until the server is ready (up to 10 s)
    for _ in range(20):
        time.sleep(0.5)
        code, _ = container.exec_run(HEALTH_CMD, demux=True)
        if code == 0:
            return

    raise RuntimeError("Browser server failed to start inside the sandbox")


def _browser_request_sync(
    sandbox_id: str, method: str, path: str, body: dict | None = None
) -> dict:
    """Send an HTTP request to the browser server inside the container."""
    container = get_container(sandbox_id)
    _ensure_browser_server(container)

    if method == "GET":
        cmd = ["curl", "-sf", f"http://localhost:8330{path}"]
    else:
        cmd = [
            "curl", "-sf",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body or {}),
            f"http://localhost:8330{path}",
        ]

    exit_code, output = container.exec_run(cmd, demux=True)
    stdout = output[0].decode("utf-8", errors="replace") if output and output[0] else ""
    stderr = output[1].decode("utf-8", errors="replace") if output and output[1] else ""

    if exit_code != 0:
        raise RuntimeError(f"Browser request failed (exit {exit_code}): {stderr or stdout}")

    return json.loads(stdout)


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def navigate(sandbox_id: str, url: str) -> dict:
    """Navigate the browser to *url* and return the page title + final URL."""
    return await asyncio.to_thread(
        _browser_request_sync, sandbox_id, "POST", "/navigate", {"url": url}
    )


async def screenshot(sandbox_id: str, full_page: bool = False) -> dict:
    """Take a screenshot and return it as a base64-encoded PNG."""
    return await asyncio.to_thread(
        _browser_request_sync, sandbox_id, "POST", "/screenshot", {"full_page": full_page}
    )


async def click(sandbox_id: str, selector: str) -> dict:
    """Click the element matching *selector*."""
    return await asyncio.to_thread(
        _browser_request_sync, sandbox_id, "POST", "/click", {"selector": selector}
    )


async def type_text(sandbox_id: str, selector: str, text: str) -> dict:
    """Fill *text* into the element matching *selector*."""
    return await asyncio.to_thread(
        _browser_request_sync, sandbox_id, "POST", "/type", {"selector": selector, "text": text}
    )


async def get_content(sandbox_id: str, mode: str = "text") -> dict:
    """Return the current page content (``text`` or ``html``)."""
    return await asyncio.to_thread(
        _browser_request_sync, sandbox_id, "GET", f"/content?mode={mode}"
    )
