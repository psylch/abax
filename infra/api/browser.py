"""Browser automation routes."""
from fastapi import APIRouter, Depends, HTTPException
from docker.errors import NotFound

from infra.auth import verify_api_key
from infra.core import browser
from infra.core.store import store
from infra.events import publish as emit_event
from infra.models import (
    BrowserClickRequest, BrowserNavigateRequest,
    BrowserScreenshotRequest, BrowserTypeRequest,
)

router = APIRouter(prefix="/sandboxes/{sandbox_id}/browser", tags=["browser"])
Auth = Depends(verify_api_key)


@router.post("/navigate")
async def browser_navigate(sandbox_id: str, req: BrowserNavigateRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        result = await browser.navigate(sandbox_id, req.url)
        await emit_event(sandbox_id, "browser.navigated", {"url": req.url})
        return result
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/screenshot")
async def browser_screenshot(sandbox_id: str, req: BrowserScreenshotRequest | None = None, _=Auth):
    try:
        store.record_activity(sandbox_id)
        full_page = (req or BrowserScreenshotRequest()).full_page
        return await browser.screenshot(sandbox_id, full_page)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/click")
async def browser_click(sandbox_id: str, req: BrowserClickRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.click(sandbox_id, req.selector)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/type")
async def browser_type(sandbox_id: str, req: BrowserTypeRequest, _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.type_text(sandbox_id, req.selector, req.text)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/content")
async def browser_content(sandbox_id: str, mode: str = "text", _=Auth):
    try:
        store.record_activity(sandbox_id)
        return await browser.get_content(sandbox_id, mode)
    except NotFound:
        raise HTTPException(404, "sandbox not found")
    except RuntimeError as e:
        raise HTTPException(502, str(e))
