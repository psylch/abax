"""Unit tests for agent.core.orchestrator — run_turn with mocked query()."""

from unittest.mock import patch, AsyncMock

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

from agent.core.orchestrator import run_turn
from tests.agent.mock_sdk import mock_query


# Shared mock for SandboxManager so we never hit real infra
def _mock_sandbox_mgr():
    mgr = AsyncMock()
    mgr.sandbox_id = "sbx-test-123"
    return mgr


@pytest.mark.asyncio
async def test_text_only_turn():
    """AssistantMessage with a single TextBlock produces text in TurnResult."""
    responses = [
        AssistantMessage(content=[TextBlock("hello")], model="mock"),
    ]
    mgr = _mock_sandbox_mgr()

    with patch("agent.core.orchestrator.query", return_value=mock_query(responses)):
        result = await run_turn("hi", "user-1", sandbox_mgr=mgr)

    assert result.text == "hello"
    assert result.tool_calls == []
    assert result.sandbox_id == "sbx-test-123"


@pytest.mark.asyncio
async def test_tool_use_turn():
    """AssistantMessage with a ToolUseBlock populates tool_calls."""
    responses = [
        AssistantMessage(
            content=[ToolUseBlock(id="tu-1", name="execute_command", input={"command": "ls"})],
            model="mock",
        ),
    ]
    mgr = _mock_sandbox_mgr()

    with patch("agent.core.orchestrator.query", return_value=mock_query(responses)):
        result = await run_turn("list files", "user-1", sandbox_mgr=mgr)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "execute_command"
    assert result.tool_calls[0]["input"] == {"command": "ls"}


@pytest.mark.asyncio
async def test_empty_response():
    """AssistantMessage with no TextBlock produces '(no response)'."""
    responses = [
        AssistantMessage(content=[], model="mock"),
    ]
    mgr = _mock_sandbox_mgr()

    with patch("agent.core.orchestrator.query", return_value=mock_query(responses)):
        result = await run_turn("nothing", "user-1", sandbox_mgr=mgr)

    assert result.text == "(no response)"


@pytest.mark.asyncio
async def test_cost_tracking():
    """cost_usd from ResultMessage propagates into TurnResult."""
    responses = [
        AssistantMessage(content=[TextBlock("done")], model="mock"),
    ]
    mgr = _mock_sandbox_mgr()

    with patch("agent.core.orchestrator.query", return_value=mock_query(responses, cost=0.05)):
        result = await run_turn("compute", "user-1", sandbox_mgr=mgr)

    assert result.cost_usd == 0.05
    assert result.num_turns == 1
