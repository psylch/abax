"""Tests for sandbox count limits."""
import pytest

from infra.core.sandbox import (
    create_sandbox,
    destroy_sandbox,
    SandboxLimitExceeded,
    MAX_SANDBOXES_PER_USER,
)


@pytest.mark.asyncio
async def test_create_sandbox_under_limit():
    """Creating a sandbox succeeds when under the limit."""
    info = await create_sandbox("test-limits-ok")
    try:
        assert info.user_id == "test-limits-ok"
        assert info.status == "running"
    finally:
        await destroy_sandbox(info.sandbox_id)


@pytest.mark.asyncio
async def test_per_user_limit_exceeded():
    """Creating more sandboxes than the per-user limit raises SandboxLimitExceeded."""
    created = []
    try:
        for i in range(MAX_SANDBOXES_PER_USER):
            info = await create_sandbox("test-limits-user")
            created.append(info.sandbox_id)

        with pytest.raises(SandboxLimitExceeded, match="maximum"):
            await create_sandbox("test-limits-user")
    finally:
        for sid in created:
            await destroy_sandbox(sid)


@pytest.mark.asyncio
async def test_per_user_limit_does_not_affect_other_users():
    """A different user can still create sandboxes when one user hits their limit."""
    created = []
    try:
        for i in range(MAX_SANDBOXES_PER_USER):
            info = await create_sandbox("test-limits-userA")
            created.append(info.sandbox_id)

        # Different user should succeed
        info = await create_sandbox("test-limits-userB")
        created.append(info.sandbox_id)
        assert info.user_id == "test-limits-userB"
    finally:
        for sid in created:
            await destroy_sandbox(sid)
