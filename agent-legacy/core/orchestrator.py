"""Core orchestrator — builds Claude SDK client, registers tools+hooks, runs agent loop."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
)
from claude_agent_sdk.types import StreamEvent

from agent.core.sandbox_mgr import SandboxManager
from agent.prompts import build_system_prompt
from agent.tools import create_sandbox_tools

logger = logging.getLogger("abax.agent.orchestrator")

MODEL = os.getenv("ABAX_AGENT_MODEL", "claude-sonnet-4-20250514")
INFRA_URL = os.getenv("ABAX_INFRA_URL", "http://localhost:8000")
INFRA_API_KEY = os.getenv("ABAX_API_KEY")

_MCP_SERVER = "abax-sandbox"
_MCP_VERSION = "1.0.0"

_RETRYABLE_KEYWORDS = frozenset([
    "overloaded", "rate limit", "rate_limit", "429",
    "500", "502", "503", "504",
    "service unavailable", "connection error", "fetch failed",
])
MAX_RETRIES = 3
BASE_DELAY = 2.0  # seconds


def _is_retryable(error: Exception) -> bool:
    """Return True if the error looks like a transient LLM / network issue."""
    msg = str(error).lower()
    return any(kw in msg for kw in _RETRYABLE_KEYWORDS)


# Env snapshot with CLAUDECODE stripped — stable for process lifetime.
_CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def make_sandbox_mgr(user_id: str) -> SandboxManager:
    """Create a SandboxManager with infra connection config."""
    return SandboxManager(user_id, infra_url=INFRA_URL, api_key=INFRA_API_KEY)


@dataclass
class TurnResult:
    """Result of a single agent turn."""
    text: str
    tool_calls: list[dict]
    sandbox_id: str | None
    cost_usd: float | None
    num_turns: int


def _build_options(
    sandbox_mgr: SandboxManager,
    user_id: str,
    history: list[dict] | None,
    **extra,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions with tools bound to sandbox_mgr."""
    tools = create_sandbox_tools(sandbox_mgr)
    server = create_sdk_mcp_server(_MCP_SERVER, _MCP_VERSION, tools)
    tool_names = [f"mcp__{_MCP_SERVER}__{t.name}" for t in tools]

    system_prompt = build_system_prompt(user_id, history=history)
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=system_prompt,
        mcp_servers={_MCP_SERVER: server},
        allowed_tools=tool_names,
        permission_mode="bypassPermissions",
        max_turns=20,
        max_budget_usd=2.0,
        env=_CLEAN_ENV,
        **extra,
    )


def _make_prompt(message: str):
    """Build AsyncIterable prompt (workaround for SDK string-prompt + MCP bug)."""
    async def _gen():
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": message},
            "parent_tool_use_id": None,
        }
    return _gen()


def _collect_blocks(msg: AssistantMessage, text_parts: list[str], tool_calls: list[dict]):
    """Extract text and tool-use blocks from an AssistantMessage."""
    for block in msg.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })


async def run_turn(
    message: str,
    user_id: str,
    sandbox_mgr: SandboxManager | None = None,
    history: list[dict] | None = None,
) -> TurnResult:
    """Run one agent turn: send message, let Claude use tools, return result.

    If sandbox_mgr is None, creates a new one (useful for CLI).
    The caller is responsible for calling sandbox_mgr.pause_if_active() after.
    """
    own_mgr = sandbox_mgr is None
    if own_mgr:
        sandbox_mgr = make_sandbox_mgr(user_id)

    try:
        options = _build_options(sandbox_mgr, user_id, history)

        for attempt in range(MAX_RETRIES + 1):
            try:
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                result_msg: ResultMessage | None = None

                # Fully consume the async generator to avoid anyio cancel scope
                # cleanup errors (generator must not be GC'd mid-iteration).
                async for msg in query(prompt=_make_prompt(message), options=options):
                    if isinstance(msg, AssistantMessage):
                        _collect_blocks(msg, text_parts, tool_calls)
                    elif isinstance(msg, ResultMessage):
                        result_msg = msg

                return TurnResult(
                    text="\n".join(text_parts) or "(no response)",
                    tool_calls=tool_calls,
                    sandbox_id=sandbox_mgr.sandbox_id,
                    cost_usd=result_msg.total_cost_usd if result_msg else None,
                    num_turns=result_msg.num_turns if result_msg else 0,
                )
            except Exception as e:
                if attempt < MAX_RETRIES and _is_retryable(e):
                    delay = BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Retryable error (attempt %d/%d), retry in %.1fs: %s",
                        attempt + 1, MAX_RETRIES, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    finally:
        if own_mgr:
            await sandbox_mgr.pause_if_active()
            await sandbox_mgr.close()


async def run_turn_streaming(
    message: str,
    user_id: str,
    sandbox_mgr: SandboxManager | None = None,
    history: list[dict] | None = None,
    session_id: str = "",
) -> AsyncGenerator[dict, None]:
    """Like run_turn but yields streaming SSE events as they happen.

    Events yielded:
        turn.start  — {session_id}
        text.delta  — {text}
        tool.start  — {tool, input}
        tool.end    — {tool}
        turn.end    — {text, tool_calls, sandbox_id, cost_usd, num_turns}
    """
    own_mgr = sandbox_mgr is None
    if own_mgr:
        sandbox_mgr = make_sandbox_mgr(user_id)

    try:
        # Queue for hook events (hooks run inside SDK, push events here)
        event_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

        async def on_pre_tool(input, tool_use_id, ctx) -> dict:
            event_queue.put_nowait({
                "event": "tool.start",
                "data": {
                    "tool": input.get("tool_name", ""),
                    "input": input.get("tool_input", {}),
                },
            })
            return {}

        async def on_post_tool(input, tool_use_id, ctx) -> dict:
            event_queue.put_nowait({
                "event": "tool.end",
                "data": {"tool": input.get("tool_name", "")},
            })
            return {}

        options = _build_options(
            sandbox_mgr, user_id, history,
            include_partial_messages=True,
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[on_pre_tool])],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[on_post_tool])],
            },
        )

        yield {"event": "turn.start", "data": {"session_id": session_id}}

        for attempt in range(MAX_RETRIES + 1):
            try:
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                result_msg: ResultMessage | None = None

                async for msg in query(prompt=_make_prompt(message), options=options):
                    # Drain hook events first
                    while not event_queue.empty():
                        yield event_queue.get_nowait()

                    if isinstance(msg, StreamEvent):
                        event = msg.event
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield {
                                    "event": "text.delta",
                                    "data": {"text": delta["text"]},
                                }

                    elif isinstance(msg, AssistantMessage):
                        _collect_blocks(msg, text_parts, tool_calls)

                    elif isinstance(msg, ResultMessage):
                        result_msg = msg

                # Drain remaining hook events
                while not event_queue.empty():
                    yield event_queue.get_nowait()

                yield {
                    "event": "turn.end",
                    "data": {
                        "text": "\n".join(text_parts) or "(no response)",
                        "tool_calls": tool_calls,
                        "sandbox_id": sandbox_mgr.sandbox_id,
                        "cost_usd": result_msg.total_cost_usd if result_msg else None,
                        "num_turns": result_msg.num_turns if result_msg else 0,
                    },
                }
                break  # success — exit retry loop
            except Exception as e:
                if attempt < MAX_RETRIES and _is_retryable(e):
                    delay = BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Retryable error (attempt %d/%d), retry in %.1fs: %s",
                        attempt + 1, MAX_RETRIES, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    finally:
        if own_mgr:
            await sandbox_mgr.pause_if_active()
            await sandbox_mgr.close()
