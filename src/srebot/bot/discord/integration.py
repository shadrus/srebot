"""Discord-specific BotIntegration implementation."""

import asyncio
import logging

import discord
from discord.ext import commands

from srebot.bot.base import BotIntegration
from srebot.bot.discord.handlers import register_handlers
from srebot.config import Settings
from srebot.llm.agent import get_agent

logger = logging.getLogger(__name__)


class DiscordBotIntegration(BotIntegration):
    """
    Discord chat platform integration.

    Reads ``discord_bot_token`` and ``discord_channel_id`` from settings.
    The integration is considered configured when ``discord_bot_token`` is
    non-empty.

    Args:
        settings: Application-wide settings instance.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._bot: commands.Bot | None = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # BotIntegration interface
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if a Discord bot token is present in settings."""
        return bool(self._settings.discord_bot_token)

    def start(self) -> None:
        """Run the Discord bot (blocking until shutdown)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._run())
        finally:
            # Lifecycle hooks handled in _run or via bot events
            pass

    async def _run(self) -> None:
        """Internal async runner for the Discord bot."""
        logger.info("Initializing Discord bot integration")

        # Basic intents: default + message_content to read alerts
        intents = discord.Intents.default()
        intents.message_content = True

        self._bot = commands.Bot(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        @self._bot.event
        async def on_ready() -> None:
            logger.info("Discord bot logged in as %s (ID: %d)", self._bot.user, self._bot.user.id)
            # Fetch latest parsing strategies upon startup
            await get_agent().refresh_strategies()
            await self._register_mcp_servers()
            logger.info("Discord integration fully initialized.")

        # Register handlers (pass bot and settings)
        register_handlers(self._bot, self._settings)

        async with self._bot:
            try:
                await self._bot.start(self._settings.discord_bot_token)
            except asyncio.CancelledError:
                logger.info("Discord bot task cancelled")
            except Exception:
                logger.exception("Discord bot died with error")
            finally:
                await self._shutdown_resources()

    def stop(self) -> None:
        """Stop the Discord application if it is running."""
        if self._bot is not None:
            logger.info("Stopping Discord bot...")
            asyncio.run_coroutine_threadsafe(self._bot.close(), self._bot.loop)
