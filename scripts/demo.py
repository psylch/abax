"""
Abax Gateway demo — manual verification script.

Prerequisites:
  1. Docker daemon running
  2. `docker build -t abax-sandbox sandbox-image/`
  3. `uvicorn gateway.main:app --port 8000`

Usage:
  python scripts/demo.py
"""

import json
import httpx
import asyncio
import websockets

BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"


def step(msg: str):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


async def main():
    async with httpx.AsyncClient(base_url=BASE) as client:
        # Health check
        step("Health check")
        r = await client.get("/health")
        print(r.json())

        # Create sandbox
        step("Create sandbox for user 'demo'")
        r = await client.post("/sandboxes", json={"user_id": "demo"})
        sandbox = r.json()
        sid = sandbox["sandbox_id"]
        print(f"Created: {sandbox}")

        # Write a beancount file
        step("Write sample beancount file")
        sample_beancount = """\
option "title" "Abax Demo Ledger"
option "operating_currency" "CNY"

2026-01-01 open Assets:Bank:Checking  CNY
2026-01-01 open Expenses:Food:Lunch   CNY
2026-01-01 open Expenses:Transport    CNY
2026-01-01 open Income:Salary         CNY

2026-01-15 * "Salary"
  Assets:Bank:Checking   10000.00 CNY
  Income:Salary

2026-01-16 * "Lunch with coworker"
  Expenses:Food:Lunch    35.00 CNY
  Assets:Bank:Checking

2026-01-17 * "Taxi to office"
  Expenses:Transport     28.00 CNY
  Assets:Bank:Checking
"""
        r = await client.put(
            f"/sandboxes/{sid}/files/data/ledger.beancount",
            json={"content": sample_beancount, "path": "/data/ledger.beancount"},
        )
        print(f"Write result: {r.json()}")

        # Read it back
        step("Read beancount file back")
        r = await client.get(f"/sandboxes/{sid}/files/data/ledger.beancount")
        print(r.json()["content"][:200] + "...")

        # Run bean-check
        step("Run bean-check (validate ledger)")
        r = await client.post(
            f"/sandboxes/{sid}/exec",
            json={"command": "bean-check /data/ledger.beancount"},
        )
        result = r.json()
        print(f"exit_code: {result['exit_code']}")
        if result["stdout"]:
            print(f"stdout: {result['stdout']}")
        if result["stderr"]:
            print(f"stderr: {result['stderr']}")

        # Run bean-query
        step("Run bean-query (list expenses)")
        r = await client.post(
            f"/sandboxes/{sid}/exec",
            json={
                "command": 'bean-query /data/ledger.beancount "SELECT date, narration, position WHERE account ~ \'Expenses\'"'
            },
        )
        result = r.json()
        print(f"exit_code: {result['exit_code']}, duration: {result['duration_ms']}ms")
        print(result["stdout"])

        # WebSocket streaming demo
        step("WebSocket streaming: run a multi-line Python script")
        ws_url = f"{WS_BASE}/sandboxes/{sid}/stream"
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "command": "python3 -c \"import time; [print(f'line {i}', flush=True) or time.sleep(0.3) for i in range(5)]\""
                        }
                    )
                )
                async for msg in ws:
                    data = json.loads(msg)
                    if data["type"] == "exit":
                        print(f"[exit code: {data['data']}]")
                        break
                    print(f"[{data['type']}] {data['data']}", end="")
        except Exception as e:
            print(f"WebSocket error: {e}")

        # Cleanup
        step("Destroy sandbox")
        r = await client.delete(f"/sandboxes/{sid}")
        print(f"Destroyed (status {r.status_code})")

        # Verify it's gone
        r = await client.get("/sandboxes")
        print(f"Remaining sandboxes: {r.json()}")

    print(f"\n{'='*60}")
    print("  Demo complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
