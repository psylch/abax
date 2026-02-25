"""Tests for agent turn execution, LLM proxy, and Tier 1/2/3 routing.

Unit tests mock the Anthropic API to test routing logic without real LLM calls.
Integration tests use the daemon's /agent/turn endpoint with mocked LLM.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

# Import daemon app for direct unit testing
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sandbox-image"))
from sandbox_server import app as daemon_app


@pytest.fixture
async def daemon_client():
    transport = ASGITransport(app=daemon_app)
    async with AsyncClient(transport=transport, base_url="http://daemon") as c:
        yield c


# ---------------------------------------------------------------------------
# LLM Proxy tests
# ---------------------------------------------------------------------------


class TestLLMProxy:
    async def test_proxy_rejects_without_api_key(self, client):
        """LLM proxy should fail if ANTHROPIC_API_KEY not set."""
        with patch("gateway.llm_proxy.ANTHROPIC_API_KEY", ""):
            r = await client.post("/llm/proxy", json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "hello"}],
            })
            assert r.status_code == 500
            assert "ANTHROPIC_API_KEY" in r.json()["detail"]

    async def test_proxy_forwards_request(self, client):
        """LLM proxy should forward to Anthropic and return response."""
        mock_response = {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
        }
        with patch("gateway.llm_proxy.ANTHROPIC_API_KEY", "test-key"), \
             patch("gateway.llm_proxy._sync_llm_request", new_callable=AsyncMock, return_value=mock_response):
            r = await client.post("/llm/proxy", json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "hello"}],
            })
            assert r.status_code == 200
            data = r.json()
            assert data["content"][0]["text"] == "Hello!"


# ---------------------------------------------------------------------------
# Tier routing tests
# ---------------------------------------------------------------------------


def _make_text_response(text: str) -> dict:
    """Create a mock Anthropic response with just text (no tool use)."""
    return {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "model": "claude-sonnet-4-20250514",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _make_tool_use_response(tool_name: str, tool_input: dict) -> dict:
    """Create a mock Anthropic response with tool_use."""
    return {
        "id": "msg_456",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me execute that."},
            {
                "type": "tool_use",
                "id": "toolu_001",
                "name": tool_name,
                "input": tool_input,
            },
        ],
        "stop_reason": "tool_use",
        "model": "claude-sonnet-4-20250514",
        "usage": {"input_tokens": 10, "output_tokens": 15},
    }


class TestTierRouting:
    async def _create_session(self, client) -> str:
        """Helper to create a session and return session_id."""
        r = await client.post("/sessions", json={"user_id": "test-agent"})
        assert r.status_code == 200
        return r.json()["session_id"]

    async def test_tier1_text_only(self, client):
        """Tier 1: LLM returns text only, no container needed."""
        session_id = await self._create_session(client)

        mock_resp = _make_text_response("This is a simple text answer.")

        with patch("gateway.agent.proxy_llm_request", new_callable=AsyncMock, return_value=mock_resp):
            r = await client.post(
                f"/sessions/{session_id}/chat",
                json={"message": "What is 2+2?"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["tier"] == "tier1"
            assert data["sandbox_id"] is None
            assert data["tool_calls_count"] == 0
            assert "simple text answer" in data["response"]

    async def test_tier1_saves_history(self, client):
        """Tier 1 should save user message and assistant response to history."""
        session_id = await self._create_session(client)

        mock_resp = _make_text_response("The answer is four.")

        with patch("gateway.agent.proxy_llm_request", new_callable=AsyncMock, return_value=mock_resp):
            await client.post(
                f"/sessions/{session_id}/chat",
                json={"message": "What is 2+2?"},
            )

        # Check history
        r = await client.get(f"/sessions/{session_id}/history")
        assert r.status_code == 200
        messages = r.json()["messages"]
        assert len(messages) >= 2
        # User message
        assert messages[-2]["role"] == "user"
        assert messages[-2]["content"] == "What is 2+2?"
        # Assistant message
        assert messages[-1]["role"] == "assistant"
        assert "four" in messages[-1]["content"]

    async def test_tier2_creates_sandbox(self, client):
        """Tier 2: tool_use detected, new sandbox created."""
        session_id = await self._create_session(client)

        tool_resp = _make_tool_use_response("execute_command", {"command": "echo hello"})
        final_resp = _make_text_response("Done! The output was 'hello'.")

        # Mock: first call returns tool_use, daemon handles the rest
        mock_proxy = AsyncMock(return_value=tool_resp)

        # Mock the daemon turn to return a completed result
        mock_daemon_result = {
            "response": "Done! The output was 'hello'.",
            "turns": [
                {"role": "assistant", "text": "Let me execute that.", "tool_calls": [
                    {"type": "tool_use", "id": "toolu_001", "name": "execute_command", "input": {"command": "echo hello"}}
                ]},
                {"role": "tool_results", "results": [
                    {"type": "tool_result", "tool_use_id": "toolu_001", "content": "hello"}
                ]},
                {"role": "assistant", "text": "Done! The output was 'hello'."},
            ],
            "tool_calls_count": 1,
        }

        with patch("gateway.agent.proxy_llm_request", mock_proxy), \
             patch("gateway.agent._run_daemon_turn", new_callable=AsyncMock, return_value=mock_daemon_result), \
             patch("gateway.agent._wait_for_daemon_ready", new_callable=AsyncMock):
            r = await client.post(
                f"/sessions/{session_id}/chat",
                json={"message": "Run echo hello"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["tier"] in ("tier2", "tier3")
            assert data["sandbox_id"] is not None
            assert data["tool_calls_count"] == 1
            assert "hello" in data["response"]

            # Clean up sandbox
            if data["sandbox_id"]:
                try:
                    # Resume first if paused
                    await client.post(f"/sandboxes/{data['sandbox_id']}/resume")
                except Exception:
                    pass
                await client.delete(f"/sandboxes/{data['sandbox_id']}")

    async def test_chat_session_not_found(self, client):
        """Chat with nonexistent session returns 404."""
        r = await client.post(
            "/sessions/nonexistent/chat",
            json={"message": "hello"},
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Daemon /agent/turn unit tests
# ---------------------------------------------------------------------------


class TestDaemonAgentTurn:
    async def test_agent_turn_text_only(self, daemon_client):
        """Agent turn with text-only first_response should return immediately."""
        first_response = _make_text_response("Just a text answer.")

        r = await daemon_client.post("/agent/turn", json={
            "messages": [{"role": "user", "content": "hello"}],
            "system_prompt": "You are a test agent.",
            "tools": [],
            "first_response": first_response,
            "model": "test-model",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["response"] == "Just a text answer."
        assert data["tool_calls_count"] == 0
        assert len(data["turns"]) == 1

    async def test_agent_turn_with_tool_use(self, daemon_client):
        """Agent turn with tool_use should execute locally and call LLM again."""
        first_response = _make_tool_use_response("execute_command", {"command": "echo test-output"})
        final_response = _make_text_response("The command output was: test-output")

        with patch("sandbox_server._call_llm", new_callable=AsyncMock, return_value=final_response):
            r = await daemon_client.post("/agent/turn", json={
                "messages": [{"role": "user", "content": "run echo test-output"}],
                "system_prompt": "You are a test agent.",
                "tools": [{"name": "execute_command", "description": "Run command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}],
                "first_response": first_response,
                "model": "test-model",
            })
            assert r.status_code == 200
            data = r.json()
            assert data["tool_calls_count"] == 1
            assert "test-output" in data["response"]
            # Should have 3 turns: assistant (tool_use), tool_results, assistant (text)
            assert len(data["turns"]) == 3

    async def test_agent_turn_file_ops(self, daemon_client, tmp_path):
        """Agent turn with file write + read tools."""
        test_file = str(tmp_path / "agent-test.txt")

        # First response: write_file
        first_response = _make_tool_use_response("write_file", {
            "path": test_file,
            "content": "agent wrote this",
        })

        # Second response: read_file
        read_response = {
            "id": "msg_789",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_002", "name": "read_file", "input": {"path": test_file}},
            ],
            "stop_reason": "tool_use",
            "model": "test-model",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }

        # Final response: text
        final_response = _make_text_response("File contents: agent wrote this")

        call_count = 0
        async def mock_llm(gateway_url, body):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return read_response
            return final_response

        with patch("sandbox_server._call_llm", side_effect=mock_llm):
            r = await daemon_client.post("/agent/turn", json={
                "messages": [{"role": "user", "content": "write and read a file"}],
                "system_prompt": "You are a test agent.",
                "tools": [],
                "first_response": first_response,
                "model": "test-model",
            })
            assert r.status_code == 200
            data = r.json()
            assert data["tool_calls_count"] == 2
            assert "agent wrote this" in data["response"]

    async def test_agent_turn_list_files(self, daemon_client, tmp_path):
        """Agent turn with list_files tool."""
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")

        first_response = _make_tool_use_response("list_files", {"path": str(tmp_path)})
        final_response = _make_text_response("Directory has a.txt and b.txt")

        with patch("sandbox_server._call_llm", new_callable=AsyncMock, return_value=final_response):
            r = await daemon_client.post("/agent/turn", json={
                "messages": [{"role": "user", "content": "list files"}],
                "system_prompt": "You are a test agent.",
                "tools": [],
                "first_response": first_response,
                "model": "test-model",
            })
            assert r.status_code == 200
            data = r.json()
            assert data["tool_calls_count"] == 1


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestAgentHelpers:
    def test_build_system_prompt_no_context(self):
        from gateway.agent import _build_system_prompt
        prompt = _build_system_prompt("nonexistent-user-xyz")
        assert "Abax" in prompt
        assert "execute_command" in prompt

    def test_history_to_anthropic_messages(self):
        from gateway.agent import _history_to_anthropic_messages

        history = [
            {"role": "user", "content": "hello", "tool_calls": None, "tool_results": None},
            {"role": "assistant", "content": "Hi!", "tool_calls": None, "tool_results": None},
        ]
        messages = _history_to_anthropic_messages(history)
        assert len(messages) == 2
        assert messages[0] == {"role": "user", "content": "hello"}
        assert messages[1] == {"role": "assistant", "content": "Hi!"}

    def test_history_with_tool_calls(self):
        from gateway.agent import _history_to_anthropic_messages

        tool_calls = json.dumps([
            {"type": "tool_use", "id": "t1", "name": "execute_command", "input": {"command": "ls"}}
        ])
        tool_results = json.dumps([
            {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"}
        ])

        history = [
            {"role": "user", "content": "list files", "tool_calls": None, "tool_results": None},
            {"role": "assistant", "content": "Let me check.", "tool_calls": tool_calls, "tool_results": None},
            {"role": "user", "content": "", "tool_calls": None, "tool_results": tool_results},
        ]
        messages = _history_to_anthropic_messages(history)
        assert len(messages) == 3
        # Assistant message should have content blocks
        assert isinstance(messages[1]["content"], list)
        assert messages[1]["content"][0]["type"] == "text"
        assert messages[1]["content"][1]["type"] == "tool_use"
        # Tool results
        assert isinstance(messages[2]["content"], list)
        assert messages[2]["content"][0]["type"] == "tool_result"

    def test_has_tool_use(self):
        from gateway.agent import _has_tool_use
        assert _has_tool_use({"content": [{"type": "tool_use", "id": "t1"}]}) is True
        assert _has_tool_use({"content": [{"type": "text", "text": "hi"}]}) is False
        assert _has_tool_use({"content": []}) is False

    def test_extract_text(self):
        from gateway.agent import _extract_text
        assert _extract_text({"content": [{"type": "text", "text": "hello"}]}) == "hello"
        assert _extract_text({"content": [
            {"type": "text", "text": "a"},
            {"type": "tool_use", "id": "t1"},
            {"type": "text", "text": "b"},
        ]}) == "a\nb"
