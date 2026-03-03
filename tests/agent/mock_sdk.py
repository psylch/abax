"""Mock async generator that yields pre-defined SDK messages."""

from claude_agent_sdk import ResultMessage


async def mock_query(responses, cost=0.01):
    """Async generator yielding pre-defined SDK messages.

    Use as: patch("agent.core.orchestrator.query", return_value=mock_query([...]))
    """
    for msg in responses:
        yield msg
    yield ResultMessage(
        subtype="query_result",
        duration_ms=100,
        duration_api_ms=50,
        is_error=False,
        num_turns=len(responses),
        session_id="mock",
        total_cost_usd=cost,
    )
