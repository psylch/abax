"""LLM Proxy — injects ANTHROPIC_API_KEY and forwards to Anthropic API.

The container daemon calls this proxy instead of Anthropic directly,
so the API key never needs to be exposed inside containers.
"""

import json
import os

import httpx
from fastapi import HTTPException
from starlette.responses import StreamingResponse

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


async def proxy_llm_request(request_body: dict, stream: bool = False):
    """Forward a request to Anthropic's messages API.

    Args:
        request_body: The Anthropic API request body (model, messages, etc.)
        stream: Whether to return a streaming response.

    Returns:
        dict for non-streaming, StreamingResponse for streaming.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured on gateway")

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    if stream:
        request_body["stream"] = True
        return await _stream_llm_request(request_body, headers)

    return await _sync_llm_request(request_body, headers)


async def _sync_llm_request(body: dict, headers: dict) -> dict:
    """Non-streaming LLM request."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(ANTHROPIC_BASE_URL, json=body, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(
                resp.status_code,
                f"Anthropic API error: {resp.text}",
            )
        return resp.json()


async def _stream_llm_request(body: dict, headers: dict) -> StreamingResponse:
    """Streaming LLM request — returns SSE passthrough."""
    client = httpx.AsyncClient(timeout=120)

    async def event_generator():
        try:
            async with client.stream(
                "POST", ANTHROPIC_BASE_URL, json=body, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {json.dumps({'type': 'error', 'error': error_body.decode()})}\n\n"
                    return
                async for line in resp.aiter_lines():
                    if line:
                        yield f"{line}\n"
                    else:
                        yield "\n"
        finally:
            await client.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
