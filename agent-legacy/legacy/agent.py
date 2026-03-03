"""Agent orchestration -- Tier 1/2/3 routing for chat requests.

Tier 1: LLM returns pure text -> respond immediately, no container needed.
Tier 2: LLM returns tool_use + no existing container -> create container, run turn.
Tier 3: LLM returns tool_use + paused container -> resume container, run turn.
"""

import asyncio
import json
import logging
import os

from docker.errors import NotFound

from gateway.context import read_user_context
from gateway.daemon import request_sync
from gateway.llm_proxy import proxy_llm_request
from gateway.sandbox import (
    create_sandbox,
    get_container,
    get_sandbox,
    list_sandboxes,
    pause_sandbox,
    resume_sandbox,
    SandboxStateError,
)
from gateway.store import store
from gateway.events import publish as emit_event

logger = logging.getLogger("abax.agent")

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

SYSTEM_PROMPT_TEMPLATE = """\
You are Abax, a helpful assistant with access to a sandboxed execution environment.

Available tools:
- execute_command: run any shell command in the sandbox
- write_file: write content to a file
- read_file: read a file
- list_files: list a directory
- browser_navigate: open a URL in the sandbox browser
- browser_screenshot: take a screenshot
- browser_content: get page text or HTML

The sandbox has Python 3.12, beancount, pandas, matplotlib, and a workspace at /workspace.
Persistent user data is at /data/.

{user_context}

Complete the user's request step by step. When done, respond with your final answer.
Respond in the same language the user uses.\
"""

TOOL_DEFINITIONS = [
    {
        "name": "execute_command",
        "description": "Run a shell command in the sandbox and return stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": "Write text content to a file in the sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories at a given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default /workspace)"},
            },
        },
    },
    {
        "name": "browser_navigate",
        "description": "Navigate the sandbox browser to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Take a screenshot of the current browser page.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_content",
        "description": "Get the text or HTML content of the current browser page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["text", "html"], "description": "Content mode"},
            },
        },
    },
]


def _build_system_prompt(user_id: str) -> str:
    """Build system prompt with user context if available."""
    context = read_user_context(user_id)
    if context:
        context_text = "User context files:\n"
        for filename, content in context.items():
            context_text += f"\n--- {filename} ---\n{content}\n"
    else:
        context_text = ""
    return SYSTEM_PROMPT_TEMPLATE.format(user_context=context_text)


def _history_to_anthropic_messages(history: list[dict]) -> list[dict]:
    """Convert stored message history to Anthropic API format.

    Stored messages have: role, content (str), tool_calls (json str), tool_results (json str).
    Anthropic expects: role, content (str or list of content blocks).
    """
    messages = []
    for msg in history:
        role = msg["role"]
        if role == "assistant" and msg.get("tool_calls"):
            # Reconstruct assistant content with tool_use blocks
            content_blocks = []
            if msg["content"]:
                content_blocks.append({"type": "text", "text": msg["content"]})
            try:
                tool_calls = json.loads(msg["tool_calls"])
                content_blocks.extend(tool_calls)
            except (json.JSONDecodeError, TypeError):
                pass
            messages.append({"role": "assistant", "content": content_blocks})
        elif role == "user" and msg.get("tool_results"):
            # Reconstruct tool_result blocks
            try:
                tool_results = json.loads(msg["tool_results"])
                messages.append({"role": "user", "content": tool_results})
            except (json.JSONDecodeError, TypeError):
                messages.append({"role": "user", "content": msg["content"]})
        else:
            messages.append({"role": role, "content": msg["content"]})
    return messages


def _has_tool_use(response: dict) -> bool:
    """Check if an Anthropic API response contains tool_use blocks."""
    return any(b.get("type") == "tool_use" for b in response.get("content", []))


def _extract_text(response: dict) -> str:
    """Extract text content from an Anthropic API response."""
    texts = [b["text"] for b in response.get("content", []) if b.get("type") == "text"]
    return "\n".join(texts)


def _extract_tool_calls(response: dict) -> list[dict]:
    """Extract tool_use blocks from an Anthropic API response."""
    return [b for b in response.get("content", []) if b.get("type") == "tool_use"]


async def _wait_for_daemon_ready(sandbox_id: str, timeout: float = 30):
    """Wait for the daemon inside a container to become healthy."""
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        try:
            container = get_container(sandbox_id)
            exit_code, _ = container.exec_run(
                ["curl", "-sf", "http://localhost:8331/health"],
                demux=True,
            )
            if exit_code == 0:
                return
        except Exception:
            pass
        await asyncio.sleep(0.3)
    raise RuntimeError(f"Daemon in sandbox {sandbox_id} did not start within {timeout}s")


async def _find_user_sandbox(user_id: str) -> str | None:
    """Find an existing sandbox for the user (running or paused)."""
    sandboxes = await list_sandboxes()
    for sb in sandboxes:
        if sb.user_id == user_id and sb.status in ("running", "paused"):
            return sb.sandbox_id
    return None


async def _ensure_sandbox(session_id: str, user_id: str) -> tuple[str, str]:
    """Ensure a running sandbox exists for this session.

    Lookup order:
    1. Session's bound container (if still alive)
    2. Any running/paused container for the user
    3. Create a new container

    Returns (sandbox_id, tier) where tier is "tier2" (new) or "tier3" (resumed).
    """
    # 1. Check session's bound container
    bound_id = store.get_session_container(session_id)
    if bound_id:
        try:
            info = await get_sandbox(bound_id)
            if info.status == "paused":
                await resume_sandbox(bound_id)
                await emit_event(bound_id, "sandbox.resumed")
                return bound_id, "tier3"
            if info.status == "running":
                return bound_id, "tier3"
        except (NotFound, SandboxStateError):
            store.clear_session_container(bound_id)

    # 2. Check for any existing user container
    existing_id = await _find_user_sandbox(user_id)
    if existing_id:
        try:
            info = await get_sandbox(existing_id)
            if info.status == "paused":
                await resume_sandbox(existing_id)
                await emit_event(existing_id, "sandbox.resumed")
                store.bind_session_container(session_id, existing_id)
                return existing_id, "tier3"
            if info.status == "running":
                store.bind_session_container(session_id, existing_id)
                return existing_id, "tier3"
        except (NotFound, SandboxStateError):
            pass

    # 3. Create new sandbox
    info = await create_sandbox(user_id)
    store.register(info.sandbox_id, user_id)
    store.bind_session_container(session_id, info.sandbox_id)
    await emit_event(info.sandbox_id, "sandbox.created", {"user_id": user_id})
    return info.sandbox_id, "tier2"


async def _run_daemon_turn(
    sandbox_id: str,
    messages: list[dict],
    system_prompt: str,
    first_response: dict,
) -> dict:
    """Forward the agent turn to the daemon's /agent/turn endpoint.

    The daemon runs the ReAct loop locally inside the container.
    """
    body = {
        "messages": messages,
        "system_prompt": system_prompt,
        "tools": TOOL_DEFINITIONS,
        "first_response": first_response,
        "model": MODEL,
    }

    return await asyncio.to_thread(
        request_sync, sandbox_id, "POST", "/agent/turn", body, timeout=300
    )


async def handle_chat_message(
    session_id: str,
    user_id: str,
    message: str,
) -> dict:
    """Handle a user chat message with Tier 1/2/3 routing.

    Returns:
        {
            "response": str,
            "tier": "tier1" | "tier2" | "tier3",
            "sandbox_id": str | None,
            "tool_calls_count": int,
        }
    """
    # 1. Load history from SQLite
    history = store.load_history(session_id)
    anthropic_messages = _history_to_anthropic_messages(history)

    # 2. Add user message
    anthropic_messages.append({"role": "user", "content": message})

    # Save user message to history
    store.save_message(session_id, "user", message)

    # 3. Build system prompt with user context
    system_prompt = _build_system_prompt(user_id)

    # 4. First LLM call to determine tier
    first_response = await proxy_llm_request({
        "model": MODEL,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": anthropic_messages,
        "tools": TOOL_DEFINITIONS,
    })

    # 5. Tier 1: pure text response -- no container needed
    if not _has_tool_use(first_response):
        response_text = _extract_text(first_response)
        store.save_message(session_id, "assistant", response_text)
        await emit_event(session_id, "chat.tier1", {"response_length": len(response_text)})
        logger.info("Tier 1 response for session %s", session_id)
        return {
            "response": response_text,
            "tier": "tier1",
            "sandbox_id": None,
            "tool_calls_count": 0,
        }

    # 6. Tier 2/3: tool_use detected -- need a container
    sandbox_id, tier = await _ensure_sandbox(session_id, user_id)
    logger.info("%s: sandbox %s for session %s", tier, sandbox_id, session_id)

    # Wait for daemon to be ready (if container was just created)
    if tier == "tier2":
        await _wait_for_daemon_ready(sandbox_id)

    await emit_event(session_id, f"chat.{tier}", {"sandbox_id": sandbox_id})

    # 7. Run agent turn inside the container
    try:
        result = await _run_daemon_turn(
            sandbox_id, anthropic_messages, system_prompt, first_response
        )
    except Exception as e:
        logger.error("Daemon turn failed: %s", e)
        response_text = _extract_text(first_response)
        store.save_message(session_id, "assistant", response_text)
        return {
            "response": f"(tool execution failed: {e})\n{response_text}",
            "tier": tier,
            "sandbox_id": sandbox_id,
            "tool_calls_count": 0,
        }

    # 8. Save final response to history
    response_text = result.get("response", "")
    tool_calls_count = result.get("tool_calls_count", 0)

    # Save complete conversation turns from daemon
    for turn in result.get("turns", []):
        if turn["role"] == "assistant":
            store.save_message(
                session_id, "assistant", turn.get("text", ""),
                tool_calls=json.dumps(turn.get("tool_calls")) if turn.get("tool_calls") else None,
            )
        elif turn["role"] == "tool_results":
            store.save_message(
                session_id, "user", "",
                tool_results=json.dumps(turn.get("results")) if turn.get("results") else None,
            )

    # Save final assistant text if not already saved in turns
    if not result.get("turns"):
        store.save_message(session_id, "assistant", response_text)

    # 9. Pause container after turn (save resources)
    try:
        await pause_sandbox(sandbox_id)
        store.record_activity(sandbox_id)
    except (SandboxStateError, NotFound):
        pass

    return {
        "response": response_text,
        "tier": tier,
        "sandbox_id": sandbox_id,
        "tool_calls_count": tool_calls_count,
    }
