"""Time Messenger-specific BotIntegration implementation."""

import asyncio
import logging
from collections.abc import Mapping

from aiotimebot import Application, Router, TimeClient

from srebot.bot.base import BotIntegration
from srebot.bot.time.handlers import TimeBotIdentity, register_handlers
from srebot.config import Settings
from srebot.llm.agent import get_agent

logger = logging.getLogger(__name__)


class TimeBotIntegration(BotIntegration):
    """Run SREBot over Time Messenger's REST API and authenticated WebSocket."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def is_configured(self) -> bool:
        """Return whether all Time server, token, and channel settings are present."""
        return bool(
            self._settings.time_base_url
            and self._settings.time_token
            and self._settings.time_channel_id
        )

    def start(self) -> None:
        """Start the Time application and block while its WebSocket is running."""
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        self._task = self._loop.create_task(self._run())
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            logger.info("Time bot shutdown completed")

    async def _run(self) -> None:
        """Initialize shared services, authenticate the Time account, and consume events."""
        try:
            await get_agent().refresh_strategies()
            await self._register_mcp_servers()

            client = TimeClient(self._settings.time_base_url, self._settings.time_token)
            router = Router()
            application = Application(client, router=router)
            async with application:
                profile = await client.raw_request("GET", "/api/v4/users/me")
                if not isinstance(profile, Mapping) or not profile.get("id"):
                    raise RuntimeError("Time /api/v4/users/me response is missing the user ID")
                identity = TimeBotIdentity(
                    user_id=str(profile["id"]),
                    username=str(profile.get("username") or ""),
                )
                register_handlers(router, self._settings, client, identity)

                logger.info(
                    "Time bot WebSocket started. Listening for alerts in channel %s as @%s",
                    self._settings.time_channel_id,
                    identity.username or identity.user_id,
                )
                await application.run()
        finally:
            await self._shutdown_resources()

    def stop(self) -> None:
        """Cancel the running Time application so aiotimebot can drain handlers."""
        if self._task is None or self._task.done():
            return
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._task.cancel)
        else:
            self._task.cancel()
