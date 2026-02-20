#!/usr/bin/env python3
"""
Abax Agent demo — run the minimal ReAct agent loop against a live sandbox.

Usage:
  ANTHROPIC_API_KEY=sk-... python scripts/demo_agent.py
  ANTHROPIC_API_KEY=sk-... python scripts/demo_agent.py --task "list files in /workspace"

Requires:
  - Gateway running: uvicorn gateway.main:app --port 8000
  - ANTHROPIC_API_KEY set
"""

import argparse
import asyncio
import os
import sys
from contextlib import suppress

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.sandbox import Sandbox
from agent.loop import run_agent

DEFAULT_TASK = (
    "Create a Python file at /workspace/fibonacci.py that prints the first 10 "
    "Fibonacci numbers, then run it and show me the output"
)


async def main():
    parser = argparse.ArgumentParser(description="Abax Agent demo")
    parser.add_argument(
        "--task",
        default=DEFAULT_TASK,
        help="Task for the agent to complete",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Gateway base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Gateway API key (default: none / dev mode)",
    )
    args = parser.parse_args()

    # Check for Anthropic API key
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable is required")
        sys.exit(1)

    print(f"Task: {args.task}")
    print(f"Gateway: {args.base_url}")
    print()

    # Create sandbox
    print("Creating sandbox...")
    sb = await Sandbox.create(
        "demo-agent",
        base_url=args.base_url,
        api_key=args.api_key,
    )
    print(f"Sandbox: {sb.sandbox_id}")
    print()

    try:
        print("Running agent loop...")
        print("-" * 60)
        result = await run_agent(args.task, sb)
        print("-" * 60)
        print()
        print("Agent result:")
        print(result)
        print()
        print("Pausing sandbox...")
        await sb.pause()
        print(f"Sandbox {sb.sandbox_id} paused. Resume with SDK or gateway API.")
    except Exception as e:
        print(f"Error: {e}")
        with suppress(Exception):
            await sb.destroy()
            print("Sandbox destroyed due to error.")
        sys.exit(1)
    finally:
        await sb._client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
