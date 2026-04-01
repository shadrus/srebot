"""Slack-specific BotIntegration implementation."""

import asyncio
import logging

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from srebot.bot.base import BotIntegration
from srebot.bot.slack.handlers import register_handlers
from srebot.config import Settings
from srebot.llm.agent import get_agent

logger = logging.getLogger(__name__)


class SlackBotIntegration(BotIntegration):
    """
    Slack bot integration using Socket Mode.

    Reads ``slack_bot_token``, ``slack_app_token``, and ``slack_channel_id``
    from settings. The integration is considered configured when both tokens
    are non-empty.

    Args:
        settings: Application-wide settings instance.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None

    # ------------------------------------------------------------------
    # BotIntegration interface
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if Slack tokens are present in settings."""
        return bool(self._settings.slack_bot_token and self._settings.slack_app_token)

    def start(self) -> None:
        """Build and run the Slack Application (blocking until shutdown)."""
        # main.py sets a new event loop before calling start(); reuse it.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._run())
        finally:
            loop.run_until_complete(self._shutdown_resources())

    async def _run(self) -> None:
        """Internal async runner for the Slack bot."""
        logger.info("Initializing Slack bot integration")
        self._app = AsyncApp(token=self._settings.slack_bot_token)
        register_handlers(self._app, self._settings)

        # Fetch latest parsing strategies upon startup
        await get_agent().refresh_strategies()

        self._handler = AsyncSocketModeHandler(self._app, self._settings.slack_app_token)

        await self._register_mcp_servers()
        logger.info("Slack bot socket mode started. Listening for alerts...")
        await self._handler.start_async()

    def stop(self) -> None:
        """Stop the Slack application if it is running."""
        try:
            if self._handler is not None:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._handler.close_async(), loop)
                else:
                    loop.run_until_complete(self._handler.close_async())
        except Exception as exc:
            logger.error("Error asking socket mode handler to close: %s", exc)
