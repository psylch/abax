"""End-to-end test exercising the full Abax infra chain via SDK + ASGI transport."""
import pytest
from httpx import ASGITransport, AsyncClient

from gateway.main import app
from sdk.sandbox import Sandbox

FIB_CODE = """\
def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

for i in range(10):
    print(fib(i))
"""

FIB_EXPECTED = "0\n1\n1\n2\n3\n5\n8\n13\n21\n34\n"


@pytest.mark.asyncio
async def test_full_e2e_lifecycle():
    """Single comprehensive test: create -> exec -> files -> pause/resume -> destroy."""

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test", timeout=60)

    # 1. Create sandbox
    r = await client.post("/sandboxes", json={"user_id": "e2e-test"})
    assert r.status_code == 200
    data = r.json()
    assert "sandbox_id" in data
    assert data["status"] == "running"
    sb = Sandbox(data["sandbox_id"], client=client)

    try:
        # 2. Execute command
        result = await sb.exec("echo hello")
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

        # 3. Write a Python file
        await sb.files.write("/workspace/fib.py", FIB_CODE)

        # 4. Read it back
        content = await sb.files.read("/workspace/fib.py")
        assert content == FIB_CODE

        # 5. Execute the Python file
        result = await sb.exec("python3 /workspace/fib.py")
        assert result["exit_code"] == 0
        assert result["stdout"] == FIB_EXPECTED

        # 6. List directory
        entries = await sb.files.list("/workspace")
        names = [e["name"] for e in entries]
        assert "fib.py" in names

        # 7. Pause sandbox
        info = await sb.pause()
        assert info["status"] == "paused"

        # 8. Resume sandbox
        info = await sb.resume()
        assert info["status"] == "running"

        # 9. Execute after resume
        result = await sb.exec("echo resumed")
        assert result["exit_code"] == 0
        assert "resumed" in result["stdout"]

    finally:
        # 10. Destroy sandbox
        await sb.destroy()

    # Verify destroyed
    try:
        r = await client.get(f"/sandboxes/{sb.sandbox_id}")
        assert r.status_code == 404
    finally:
        await client.aclose()
