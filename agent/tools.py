"""
Abax Agent tools — bash + render (legacy) and SDK-based tools.

bash: execute anything in the sandbox (the core primitive)
render: tell the frontend to display a UI component (table, chart)

SDK tools: execute_command, write_file, read_file, list_files,
           browser_navigate, browser_screenshot, browser_content
"""

import json

import httpx

from sdk.sandbox import Sandbox


def _format_exec_result(result: dict) -> str:
    """Format an exec result dict (stdout, stderr, exit_code) into a readable string."""
    parts = []
    if result["stdout"]:
        parts.append(result["stdout"])
    if result["stderr"]:
        parts.append(f"[stderr] {result['stderr']}")
    if result["exit_code"] != 0:
        parts.append(f"[exit code: {result['exit_code']}]")
    return "\n".join(parts) if parts else "(no output)"

# ---------------------------------------------------------------------------
# Legacy tool definitions (used by server.py / cli.py)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "bash",
        "description": (
            "Execute a bash command in the sandbox. Use it for everything.\n"
            "Environment: Python 3.12, beancount, pandas, matplotlib. Persistent data at /data/.\n"
            "Examples:\n"
            "  - Read file: cat /data/ledger.beancount\n"
            "  - Write file: cat <<'EOF' > /data/ledger.beancount\n...\nEOF\n"
            "  - Validate: bean-check /data/ledger.beancount\n"
            "  - Query: bean-query /data/ledger.beancount \"SELECT ...\"\n"
            "  - Python: python3 -c 'import pandas; ...'\n"
            "  - Multi-line script: python3 <<'EOF'\n...\nEOF"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "render",
        "description": (
            "Render a UI component for the user. Use this when data is better shown "
            "as a table or chart instead of plain text.\n"
            "Component types:\n"
            "  - table: {columns: string[], rows: string[][]}\n"
            "  - chart: {chart_type: 'bar'|'line'|'pie', labels: string[], "
            "datasets: [{label: string, values: number[]}]}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "enum": ["table", "chart"],
                    "description": "The type of UI component to render",
                },
                "title": {
                    "type": "string",
                    "description": "Title displayed above the component",
                },
                "data": {
                    "type": "object",
                    "description": "Component-specific data (see description for schema)",
                },
            },
            "required": ["component", "data"],
        },
    },
]


class ToolContext:
    """Holds sandbox_id and gateway URL for tool execution."""

    def __init__(self, sandbox_id: str, gateway_url: str = "http://localhost:8000"):
        self.sandbox_id = sandbox_id
        self.gateway_url = gateway_url
        self.client = httpx.AsyncClient(base_url=gateway_url, timeout=60.0)

    async def close(self):
        await self.client.aclose()


async def bash(params: dict, ctx: ToolContext) -> str:
    """Execute a bash command in the sandbox."""
    r = await ctx.client.post(
        f"/sandboxes/{ctx.sandbox_id}/exec",
        json={"command": params["command"]},
    )
    if r.status_code == 404:
        return "Error: sandbox not found"
    return _format_exec_result(r.json())


async def render(params: dict, ctx: ToolContext) -> str:
    """Render a UI component. Returns confirmation (actual rendering happens on frontend)."""
    component = params.get("component", "unknown")
    title = params.get("title", "")
    return f"Rendered {component}: {title}"


TOOL_DISPATCH = {
    "bash": bash,
    "render": render,
}

# ---------------------------------------------------------------------------
# SDK-based tool definitions (used by run_agent / demo_agent)
# ---------------------------------------------------------------------------

SDK_TOOL_DEFINITIONS = [
    {
        "name": "execute_command",
        "description": "Run a shell command in the sandbox and return stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
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
                "path": {
                    "type": "string",
                    "description": "Absolute file path in the sandbox",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write",
                },
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
                "path": {
                    "type": "string",
                    "description": "Absolute file path in the sandbox",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories at a given path in the sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (default /workspace)",
                },
            },
        },
    },
    {
        "name": "browser_navigate",
        "description": "Navigate the sandbox browser to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Take a screenshot of the current browser page.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "browser_content",
        "description": "Get the text or HTML content of the current browser page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["text", "html"],
                    "description": "Content mode: 'text' or 'html' (default 'text')",
                },
            },
        },
    },
]


async def sdk_execute_command(params: dict, sb: Sandbox) -> str:
    """Run a shell command via the SDK."""
    result = await sb.exec(params["command"], timeout=params.get("timeout", 30))
    return _format_exec_result(result)


async def sdk_write_file(params: dict, sb: Sandbox) -> str:
    """Write a file via the SDK."""
    await sb.files.write(params["path"], params["content"])
    return f"Wrote {len(params['content'])} characters to {params['path']}"


async def sdk_read_file(params: dict, sb: Sandbox) -> str:
    """Read a file via the SDK."""
    return await sb.files.read(params["path"])


async def sdk_list_files(params: dict, sb: Sandbox) -> str:
    """List directory via the SDK."""
    entries = await sb.files.list(params.get("path", "/workspace"))
    if not entries:
        return "(empty directory)"
    lines = []
    for e in entries:
        kind = "dir" if e.get("is_dir") else "file"
        lines.append(f"  {kind}  {e['name']}  {e.get('size', '')}")
    return "\n".join(lines)


async def sdk_browser_navigate(params: dict, sb: Sandbox) -> str:
    """Navigate browser via the SDK."""
    result = await sb.browser.navigate(params["url"])
    return json.dumps(result)


async def sdk_browser_screenshot(params: dict, sb: Sandbox) -> str:
    """Take browser screenshot via the SDK. Returns metadata only (base64 data too large)."""
    result = await sb.browser.screenshot()
    return f"Screenshot taken ({result.get('format', 'png')}, {len(result.get('data_b64', ''))} base64 chars)"


async def sdk_browser_content(params: dict, sb: Sandbox) -> str:
    """Get browser page content via the SDK."""
    mode = params.get("mode", "text")
    result = await sb.browser.content(mode=mode)
    content = result.get("content", "")
    # Truncate if very large
    if len(content) > 10000:
        content = content[:10000] + "\n... (truncated)"
    return content


SDK_TOOL_DISPATCH = {
    "execute_command": sdk_execute_command,
    "write_file": sdk_write_file,
    "read_file": sdk_read_file,
    "list_files": sdk_list_files,
    "browser_navigate": sdk_browser_navigate,
    "browser_screenshot": sdk_browser_screenshot,
    "browser_content": sdk_browser_content,
}
