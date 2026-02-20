"""
Abax Agent CLI — interactive REPL.

Usage:
  1. Start gateway: uvicorn gateway.main:app --port 8000
  2. Run: python -m agent.cli
"""

import asyncio
import os
import sys
from pathlib import Path

import anthropic
import httpx


def load_dotenv():
    """Load .env file from project root."""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

from agent.loop import run_turn
from agent.session import Session
from agent.tools import ToolContext

GATEWAY_URL = os.getenv("ABAX_GATEWAY_URL", "http://localhost:8000")


async def ensure_sandbox(gateway_url: str) -> str:
    """Create or reuse a sandbox. Returns sandbox_id."""
    async with httpx.AsyncClient(base_url=gateway_url, timeout=30.0) as client:
        # Check for existing sandboxes
        r = await client.get("/sandboxes")
        sandboxes = r.json()
        running = [s for s in sandboxes if s["status"] == "running"]

        if running:
            sid = running[0]["sandbox_id"]
            print(f"  Reusing sandbox: {sid}")
            return sid

        # Create new one
        r = await client.post("/sandboxes", json={"user_id": "cli"})
        info = r.json()
        print(f"  Created sandbox: {info['sandbox_id']}")
        return info["sandbox_id"]


def print_sessions():
    """Print list of saved sessions."""
    sessions = Session.list_sessions()
    if not sessions:
        print("  No saved sessions.")
        return
    for s in sessions:
        preview = s["preview"] or "(empty)"
        print(f"  {s['id']}  {preview}")


async def main():
    load_dotenv()

    # Check gateway health
    try:
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=5.0) as client:
            r = await client.get("/health")
            if r.status_code != 200:
                print(f"Gateway not healthy: {r.status_code}")
                sys.exit(1)
    except httpx.ConnectError:
        print(f"Cannot connect to gateway at {GATEWAY_URL}")
        print("Start it with: uvicorn gateway.main:app --port 8000")
        sys.exit(1)

    # Check API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Setup — support custom base_url for third-party providers
    base_url = os.getenv("ANTHROPIC_BASE_URL")
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
    ) if base_url else anthropic.Anthropic(api_key=api_key)
    sandbox_id = await ensure_sandbox(GATEWAY_URL)
    session = Session()
    ctx = ToolContext(sandbox_id, GATEWAY_URL)

    print(f"\nAbax Agent (session: {session.id}, sandbox: {sandbox_id})")
    print("Commands: /new, /sessions, /load <id>, /quit")
    print()

    try:
        while True:
            try:
                user_input = input("> ").strip()
            except EOFError:
                break

            if not user_input:
                continue

            # Handle commands
            if user_input == "/quit":
                break
            elif user_input == "/new":
                session = Session()
                print(f"  New session: {session.id}")
                continue
            elif user_input == "/sessions":
                print_sessions()
                continue
            elif user_input.startswith("/load "):
                sid = user_input.split(" ", 1)[1].strip()
                session = Session.load(sid)
                print(f"  Loaded session: {session.id} ({len(session.messages)} messages)")
                continue
            elif user_input.startswith("/"):
                print(f"  Unknown command: {user_input}")
                continue

            # Add user message and run agent
            session.add_message("user", user_input)

            try:
                blocks = await run_turn(session, ctx, client)
                for b in blocks:
                    if b["type"] == "text":
                        print(b["text"])
                    elif b["type"] == "bash":
                        print(f"\n  $ {b['command']}")
                        preview = b["output"][:300] + "..." if len(b["output"]) > 300 else b["output"]
                        print(f"  {preview}\n")
                    elif b["type"] == "render":
                        print(f"\n  [{b['component']}] {b.get('title', '')}")
                        print(f"  (data: {len(str(b['data']))} chars)\n")
            except anthropic.APIError as e:
                print(f"\n  API error: {e}")
            except httpx.HTTPError as e:
                print(f"\n  Gateway error: {e}")

            print()

    except KeyboardInterrupt:
        print("\n")
    finally:
        await ctx.close()
        print(f"Session saved: {session.id}")


if __name__ == "__main__":
    asyncio.run(main())
