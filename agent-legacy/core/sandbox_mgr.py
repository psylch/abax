"""Sandbox lifecycle management — lazy creation, resume, pause."""

import logging

from sdk.sandbox import Sandbox

logger = logging.getLogger("abax.agent.sandbox")


class SandboxManager:
    """Manages a single session's sandbox lifecycle.

    - ensure_sandbox(): lazy-creates or resumes a sandbox on first tool call
    - pause_if_active(): pauses the sandbox after a turn completes
    """

    def __init__(
        self,
        user_id: str,
        *,
        infra_url: str = "http://localhost:8000",
        api_key: str | None = None,
    ):
        self.user_id = user_id
        self._infra_url = infra_url
        self._api_key = api_key
        self._sandbox: Sandbox | None = None
        self._sandbox_id: str | None = None

    @property
    def sandbox_id(self) -> str | None:
        return self._sandbox_id

    @property
    def has_sandbox(self) -> bool:
        return self._sandbox is not None

    async def ensure_sandbox(self) -> Sandbox:
        """Return an active sandbox, creating or resuming as needed."""
        if self._sandbox is not None:
            try:
                status = await self._sandbox.status()
                if status.get("status") == "paused":
                    logger.info("Resuming paused sandbox %s", self._sandbox_id)
                    await self._sandbox.resume()
                return self._sandbox
            except Exception:
                logger.warning("Lost connection to sandbox %s, recreating", self._sandbox_id)
                self._sandbox = None
                self._sandbox_id = None

        # Try to reconnect to a known sandbox
        if self._sandbox_id:
            try:
                self._sandbox = await Sandbox.connect(
                    self._sandbox_id,
                    base_url=self._infra_url,
                    api_key=self._api_key,
                )
                status = await self._sandbox.status()
                if status.get("status") == "paused":
                    await self._sandbox.resume()
                logger.info("Reconnected to sandbox %s", self._sandbox_id)
                return self._sandbox
            except Exception:
                logger.warning("Cannot reconnect to sandbox %s", self._sandbox_id)
                self._sandbox_id = None

        # Create new sandbox
        self._sandbox = await Sandbox.create(
            self.user_id,
            base_url=self._infra_url,
            api_key=self._api_key,
        )
        self._sandbox_id = self._sandbox.sandbox_id
        logger.info("Created sandbox %s for user %s", self._sandbox_id, self.user_id)
        return self._sandbox

    def bind(self, sandbox_id: str) -> None:
        """Bind to an existing sandbox ID (loaded from store)."""
        self._sandbox_id = sandbox_id

    async def pause_if_active(self) -> None:
        """Pause the sandbox to save resources. Called after turn ends."""
        if self._sandbox is None:
            return
        try:
            await self._sandbox.pause()
            logger.info("Paused sandbox %s", self._sandbox_id)
        except Exception as e:
            logger.warning("Failed to pause sandbox %s: %s", self._sandbox_id, e)

    async def close(self) -> None:
        """Close the HTTP client (does not destroy the sandbox)."""
        if self._sandbox is not None:
            await self._sandbox.__aexit__(None, None, None)
            self._sandbox = None
