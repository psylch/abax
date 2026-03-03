"""System prompt templates and user context injection."""

import os
from pathlib import Path

PERSISTENT_ROOT = os.getenv("ABAX_PERSISTENT_ROOT", "/tmp/abax-data")

SYSTEM_PROMPT = """\
You are Abax, a helpful assistant with access to a sandboxed execution environment.

You have tools to execute commands, read/write files, list directories, and control \
a browser inside the sandbox. The sandbox runs Python 3.12 with beancount, pandas, \
matplotlib, and Playwright+Chromium pre-installed.

Key paths:
- /workspace/ — working directory for the current task
- /data/ — persistent user data (survives across sessions)

Guidelines:
- Use execute_command for any computation, data processing, or package installation.
- Use file tools for reading/writing data files.
- Use browser tools when you need to interact with web pages.
- Be concise in your responses. Show results, not process.
- If a command fails, diagnose the error and try a different approach.

{user_context}"""


def read_user_context(user_id: str) -> str:
    """Read all .md files from {PERSISTENT_ROOT}/{user_id}/context/.

    Returns formatted string for injection into system prompt.
    """
    context_dir = (Path(PERSISTENT_ROOT) / user_id / "context").resolve()
    if not str(context_dir).startswith(str(Path(PERSISTENT_ROOT).resolve())):
        return ""
    if not context_dir.is_dir():
        return ""

    parts = []
    for entry in sorted(context_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".md":
            try:
                content = entry.read_text(encoding="utf-8")
                parts.append(f"## {entry.stem}\n{content}")
            except OSError:
                continue

    if not parts:
        return ""
    return "User context:\n" + "\n\n".join(parts)


def _mask_tool_output(content: str, max_len: int = 200) -> str:
    """Truncate long content from old assistant messages (tool outputs).

    Observation masking (JetBrains Research): keep reasoning intact,
    only truncate verbose tool outputs in older turns.
    """
    if len(content) <= max_len:
        return content
    return content[:max_len] + "\n[output truncated]"


def format_history(
    history: list[dict] | None, keep_recent: int = 6
) -> str:
    """Format conversation history for system prompt injection.

    Observation masking strategy:
    - Recent messages (last `keep_recent`): kept verbatim
    - Older user messages: always kept verbatim (preserve intent)
    - Older assistant messages: long content truncated (tool outputs are verbose)
    """
    if not history:
        return ""
    parts = []
    cutoff = len(history) - keep_recent
    for idx, msg in enumerate(history):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # Only mask old assistant messages — user messages always kept intact
        if idx < cutoff and role == "assistant":
            content = _mask_tool_output(content)
        parts.append(f"[{role}]: {content}")
    return "Conversation history:\n" + "\n".join(parts) + "\n\nContinue the conversation."


def build_system_prompt(
    user_id: str, history: list[dict] | None = None
) -> str:
    """Build the complete system prompt with user context and history."""
    ctx = read_user_context(user_id)
    hist = format_history(history)
    prompt = SYSTEM_PROMPT.format(user_context=ctx)
    if hist:
        prompt += "\n\n" + hist
    return prompt
