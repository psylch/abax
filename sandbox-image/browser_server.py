"""Browser automation server running inside the sandbox container.

Manages a persistent Playwright browser instance and exposes it over HTTP
on port 8330. Started on-demand by the gateway via `docker exec`.
"""

import asyncio
import base64
from contextlib import asynccontextmanager

from fastapi import FastAPI
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Global browser state
# ---------------------------------------------------------------------------

_playwright = None
_browser = None
_page = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser, _page
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    _page = await _browser.new_page()
    yield
    await _browser.close()
    await _playwright.stop()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/navigate")
async def navigate(req: dict):
    url = req["url"]
    await _page.goto(url, wait_until="domcontentloaded", timeout=30000)
    return {"title": await _page.title(), "url": _page.url}


@app.post("/screenshot")
async def screenshot(req: dict = {}):
    data = await _page.screenshot(full_page=req.get("full_page", False))
    return {"data_b64": base64.b64encode(data).decode(), "format": "png"}


@app.post("/click")
async def click(req: dict):
    selector = req["selector"]
    await _page.click(selector, timeout=10000)
    return {"ok": True}


@app.post("/type")
async def type_text(req: dict):
    selector = req["selector"]
    text = req["text"]
    await _page.fill(selector, text, timeout=10000)
    return {"ok": True}


@app.get("/content")
async def get_content(mode: str = "text"):
    if mode == "html":
        content = await _page.content()
    else:
        content = await _page.inner_text("body")
    return {"content": content, "url": _page.url, "title": await _page.title()}


@app.get("/health")
async def health():
    return {"ok": True, "has_page": _page is not None}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8330)
