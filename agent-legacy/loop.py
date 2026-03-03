"""
Abax Agent loop — minimal ReAct tool-use pattern.

Two modes:
1. Legacy streaming loop (run_turn_stream / run_turn) — used by server.py / cli.py
2. SDK-based run_agent() — minimal loop for infra validation via demo_agent.py
"""

import json
import os
from collections.abc import AsyncGenerator

import anthropic

from agent.tools import (
    TOOL_DEFINITIONS,
    TOOL_DISPATCH,
    SDK_TOOL_DEFINITIONS,
    SDK_TOOL_DISPATCH,
    ToolContext,
)
from agent.session import Session
from sdk.sandbox import Sandbox

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# ---------------------------------------------------------------------------
# Legacy streaming loop (server.py / cli.py)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Abax, a bookkeeping assistant powered by beancount.

You have two tools:
1. bash — execute any command in the sandbox. This is your core tool.
2. render — display data as a table or chart for the user.

Environment (sandbox container):
- Python 3.12, beancount, pandas, matplotlib
- Persistent storage at /data/
- beancount CLI: bean-check, bean-query, bean-report, bean-format

Workflow:
- Write files with heredoc: cat <<'EOF' > /data/file.beancount
- Read files with: cat /data/file.beancount
- Validate with: bean-check /data/ledger.beancount
- Query with: bean-query /data/ledger.beancount "SELECT ..."

When showing query results to the user, use the render tool with component="table" or "chart".
Table data format: {"columns": ["col1", "col2"], "rows": [["val1", "val2"], ...]}
Chart data format: {"chart_type": "bar"|"line"|"pie", "labels": [...], "datasets": [{"label": "...", "values": [...]}]}

Always validate with bean-check after writing beancount files.
Keep the ledger at /data/ledger.beancount unless the user specifies otherwise.

Respond in the same language the user uses (Chinese or English).\
"""


async def run_turn_stream(
    session: Session,
    ctx: ToolContext,
    client: anthropic.Anthropic,
) -> AsyncGenerator[dict, None]:
    """Run one agent turn with true streaming.

    Yields:
      {"type": "text_delta", "text": "partial..."}   — incremental text
      {"type": "bash", "command": "...", "output": "..."}  — after execution
      {"type": "render", "component": ..., "data": ...}   — render block
      {"type": "done"}
    """

    while True:
        # Use streaming API for incremental text output
        collected_content = []  # For session history
        tool_calls = []  # Accumulate tool_use blocks
        stop_reason = None
        current_text = ""

        with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=session.messages,
            tools=TOOL_DEFINITIONS,
        ) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "text":
                        current_text = ""
                    elif event.content_block.type == "tool_use":
                        tool_calls.append({
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "input_json": "",
                        })
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        current_text += event.delta.text
                        yield {"type": "text_delta", "text": event.delta.text}
                    elif event.delta.type == "input_json_delta":
                        if tool_calls:
                            tool_calls[-1]["input_json"] += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if current_text:
                        collected_content.append({"type": "text", "text": current_text})
                        current_text = ""

            # Get the final message for stop_reason
            final = stream.get_final_message()
            stop_reason = final.stop_reason

        # Parse tool call inputs and add to collected_content
        for tc in tool_calls:
            try:
                tc["input"] = json.loads(tc["input_json"]) if tc["input_json"] else {}
            except json.JSONDecodeError:
                tc["input"] = {}
            collected_content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })

        session.add_message("assistant", collected_content)

        if stop_reason == "end_turn" or not tool_calls:
            break

        # Dispatch tool calls
        tool_results = []
        for tc in tool_calls:
            handler = TOOL_DISPATCH.get(tc["name"])
            if handler:
                result = await handler(tc["input"], ctx)
            else:
                result = f"Unknown tool: {tc['name']}"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": result,
            })

            if tc["name"] == "bash":
                yield {
                    "type": "bash",
                    "command": tc["input"].get("command", ""),
                    "output": result,
                }
            elif tc["name"] == "render":
                yield {
                    "type": "render",
                    "component": tc["input"].get("component", ""),
                    "title": tc["input"].get("title", ""),
                    "data": tc["input"].get("data", {}),
                }

        session.add_message("user", tool_results)

    session.save()
    yield {"type": "done"}


async def run_turn(
    session: Session,
    ctx: ToolContext,
    client: anthropic.Anthropic,
) -> list[dict]:
    """Non-streaming version: collects all blocks and returns them."""
    blocks = []
    current_text = ""
    async for block in run_turn_stream(session, ctx, client):
        if block["type"] == "text_delta":
            current_text += block["text"]
        elif block["type"] == "done":
            if current_text:
                blocks.append({"type": "text", "text": current_text})
        else:
            # Flush accumulated text before other block types
            if current_text:
                blocks.append({"type": "text", "text": current_text})
                current_text = ""
            blocks.append(block)
    return blocks


# ---------------------------------------------------------------------------
# SDK-based agent loop (run_agent) — minimal ReAct for infra validation
# ---------------------------------------------------------------------------

SDK_SYSTEM_PROMPT = """\
You are Abax, an agent running inside a sandboxed container.

Available tools:
- execute_command: run any shell command
- write_file: write content to a file
- read_file: read a file
- list_files: list a directory
- browser_navigate: open a URL in the sandbox browser
- browser_screenshot: take a screenshot
- browser_content: get page text or HTML

The sandbox has Python 3.12, common packages, and a workspace at /workspace.
Complete the user's task step by step. When done, respond with your final answer.\
"""

MAX_TURNS = 20


async def run_agent(
    task: str,
    sb: Sandbox,
    *,
    model: str | None = None,
    max_turns: int = MAX_TURNS,
) -> str:
    """Run a minimal ReAct agent loop using the SDK.

    Args:
        task: The user's task description.
        sb: A connected Sandbox instance.
        model: Claude model to use (defaults to MODEL env var).
        max_turns: Maximum tool-use round trips.

    Returns:
        The agent's final text response.
    """
    use_model = model or MODEL
    aclient = anthropic.AsyncAnthropic()

    messages = [{"role": "user", "content": task}]

    for _ in range(max_turns):
        response = await aclient.messages.create(
            model=use_model,
            max_tokens=4096,
            system=SDK_SYSTEM_PROMPT,
            messages=messages,
            tools=SDK_TOOL_DEFINITIONS,
        )

        # Serialize assistant content for message history
        assistant_content = []
        tool_use_blocks = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                tool_use_blocks.append(block)

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn" or not tool_use_blocks:
            texts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(texts) if texts else "(no response)"

        # Dispatch tool calls
        tool_results = []
        for block in tool_use_blocks:
            handler = SDK_TOOL_DISPATCH.get(block.name)
            if handler:
                try:
                    result = await handler(block.input, sb)
                except Exception as e:
                    result = f"Error: {e}"
            else:
                result = f"Unknown tool: {block.name}"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    return "(max turns reached)"
