"""Tests for the universal sandbox daemon and gateway daemon-based routing.

Unit tests: test the daemon FastAPI app directly (no Docker needed).
Integration tests: test Gateway → daemon routing through real containers.
"""

import pytest
from httpx import AsyncClient, ASGITransport

# ---------------------------------------------------------------------------
# Unit tests — test daemon app directly (no Docker)
# ---------------------------------------------------------------------------

# Import the daemon app for direct testing
import sys
import os

# Add sandbox-image to path so we can import sandbox_server
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sandbox-image"))
from sandbox_server import app as daemon_app


@pytest.fixture
async def daemon_client():
    transport = ASGITransport(app=daemon_app)
    async with AsyncClient(transport=transport, base_url="http://daemon") as c:
        yield c


class TestDaemonHealth:
    async def test_health(self, daemon_client):
        r = await daemon_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"


class TestDaemonExec:
    async def test_exec_echo(self, daemon_client):
        r = await daemon_client.post("/exec", json={"command": "echo hello", "timeout": 5})
        assert r.status_code == 200
        data = r.json()
        assert data["stdout"].strip() == "hello"
        assert data["exit_code"] == 0

    async def test_exec_stderr(self, daemon_client):
        r = await daemon_client.post("/exec", json={"command": "echo err >&2", "timeout": 5})
        assert r.status_code == 200
        data = r.json()
        assert "err" in data["stderr"]

    async def test_exec_nonzero_exit(self, daemon_client):
        r = await daemon_client.post("/exec", json={"command": "exit 42", "timeout": 5})
        assert r.status_code == 200
        data = r.json()
        assert data["exit_code"] == 42

    async def test_exec_timeout(self, daemon_client):
        r = await daemon_client.post("/exec", json={"command": "sleep 10", "timeout": 1})
        assert r.status_code == 200
        data = r.json()
        assert data["exit_code"] == 124
        assert "timed out" in data["stderr"]


class TestDaemonFiles:
    async def test_write_and_read(self, daemon_client, tmp_path):
        test_file = str(tmp_path / "test.txt")
        # Write
        r = await daemon_client.put(
            f"/files{test_file}",
            json={"content": "hello world"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True

        # Read
        r = await daemon_client.get(f"/files{test_file}")
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "hello world"
        assert data["path"] == test_file

    async def test_read_not_found(self, daemon_client):
        r = await daemon_client.get("/files/nonexistent_file_xyz_12345")
        assert r.status_code == 200
        data = r.json()
        assert "error" in data

    async def test_write_creates_dirs(self, daemon_client, tmp_path):
        test_file = str(tmp_path / "sub" / "dir" / "file.txt")
        r = await daemon_client.put(
            f"/files{test_file}",
            json={"content": "nested"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r = await daemon_client.get(f"/files{test_file}")
        assert r.json()["content"] == "nested"

    async def test_list_dir(self, daemon_client, tmp_path):
        # Create some files
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bb")
        (tmp_path / "subdir").mkdir()

        r = await daemon_client.get(f"/ls{tmp_path}")
        assert r.status_code == 200
        data = r.json()
        assert data["path"] == str(tmp_path)
        names = [e["name"] for e in data["entries"]]
        assert "a.txt" in names
        assert "b.txt" in names
        assert "subdir" in names

        # Check dir flag
        subdir_entry = next(e for e in data["entries"] if e["name"] == "subdir")
        assert subdir_entry["is_dir"] is True

    async def test_list_dir_not_found(self, daemon_client):
        r = await daemon_client.get("/ls/nonexistent_dir_xyz_12345")
        assert r.status_code == 200
        data = r.json()
        assert "error" in data


class TestDaemonBatch:
    async def test_batch_read_write(self, daemon_client, tmp_path):
        file1 = str(tmp_path / "f1.txt")
        file2 = str(tmp_path / "f2.txt")

        r = await daemon_client.post("/files/batch", json={
            "operations": [
                {"op": "write", "path": file1, "content": "content1"},
                {"op": "write", "path": file2, "content": "content2"},
                {"op": "read", "path": file1},
                {"op": "read", "path": file2},
                {"op": "list", "path": str(tmp_path)},
            ]
        })
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 5

        # Write results
        assert results[0]["ok"] is True
        assert results[1]["ok"] is True

        # Read results
        assert results[2]["ok"] is True
        assert results[2]["content"] == "content1"
        assert results[3]["ok"] is True
        assert results[3]["content"] == "content2"

        # List result
        assert results[4]["ok"] is True
        names = [e["name"] for e in results[4]["entries"]]
        assert "f1.txt" in names
        assert "f2.txt" in names

    async def test_batch_unknown_op(self, daemon_client):
        r = await daemon_client.post("/files/batch", json={
            "operations": [
                {"op": "delete", "path": "/tmp/x"},
            ]
        })
        assert r.status_code == 200
        results = r.json()["results"]
        assert results[0]["ok"] is False
        assert "unknown op" in results[0]["error"]


# ---------------------------------------------------------------------------
# Integration tests — Gateway → daemon through real containers
# ---------------------------------------------------------------------------


class TestGatewayDaemonIntegration:
    """Integration tests that create a real container with the daemon running."""

    async def test_exec_via_gateway(self, client, sandbox_id):
        r = await client.post(
            f"/sandboxes/{sandbox_id}/exec",
            json={"command": "echo daemon-works", "timeout": 5},
        )
        assert r.status_code == 200
        data = r.json()
        assert "daemon-works" in data["stdout"]
        assert data["exit_code"] == 0

    async def test_file_write_read_via_gateway(self, client, sandbox_id):

        # Write
        r = await client.put(
            f"/sandboxes/{sandbox_id}/files/workspace/test.txt",
            json={"content": "daemon file test", "path": "/workspace/test.txt"},
        )
        assert r.status_code == 200

        # Read
        r = await client.get(f"/sandboxes/{sandbox_id}/files/workspace/test.txt")
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "daemon file test"

    async def test_list_dir_via_gateway(self, client, sandbox_id):

        # Write a file first
        await client.put(
            f"/sandboxes/{sandbox_id}/files/workspace/ls-test.txt",
            json={"content": "x", "path": "/workspace/ls-test.txt"},
        )
        r = await client.get(f"/sandboxes/{sandbox_id}/ls/workspace")
        assert r.status_code == 200
        data = r.json()
        names = [e["name"] for e in data["entries"]]
        assert "ls-test.txt" in names

    async def test_batch_files_via_gateway(self, client, sandbox_id):

        r = await client.post(
            f"/sandboxes/{sandbox_id}/files-batch",
            json={
                "operations": [
                    {"op": "write", "path": "/workspace/b1.txt", "content": "batch1"},
                    {"op": "write", "path": "/workspace/b2.txt", "content": "batch2"},
                    {"op": "read", "path": "/workspace/b1.txt"},
                    {"op": "list", "path": "/workspace"},
                ]
            },
        )
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 4
        assert results[0]["ok"] is True
        assert results[1]["ok"] is True
        assert results[2]["ok"] is True
        assert results[2]["content"] == "batch1"

    async def test_get_container_ip(self, sandbox_id):
        from gateway.sandbox import get_container_ip
        ip = await get_container_ip(sandbox_id)
        # Should be a valid IP-like string
        parts = ip.split(".")
        assert len(parts) == 4
        for p in parts:
            assert p.isdigit()

    async def test_exec_multiline_output(self, client, sandbox_id):

        r = await client.post(
            f"/sandboxes/{sandbox_id}/exec",
            json={"command": "echo -e 'line1\\nline2\\nline3'", "timeout": 5},
        )
        assert r.status_code == 200
        data = r.json()
        assert "line1" in data["stdout"]
        assert "line3" in data["stdout"]
