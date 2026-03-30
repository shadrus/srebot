"""Telegram-specific BotIntegration implementation."""

import logging

from telegram.error import NetworkError, TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from srebot.bot.base import BotIntegration
from srebot.bot.telegram.handlers import channel_post_handler
from srebot.config import Settings, get_mcp_registry
from srebot.mcp.registry import register_external_mcp, shutdown_mcp
from srebot.state.store import get_store

logger = logging.getLogger(__name__)


class TelegramBotIntegration(BotIntegration):
    """
    Telegram chat platform integration.

    Reads ``telegram_bot_token`` and ``telegram_channel_id`` from settings.
    The integration is considered configured when ``telegram_bot_token`` is
    non-empty.

    Args:
        settings: Application-wide settings instance.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._app = None  # python-telegram-bot Application, built in start()

    # ------------------------------------------------------------------
    # BotIntegration interface
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if a Telegram bot token is present in settings."""
        return bool(self._settings.telegram_bot_token)

    def start(self) -> None:
        """Build and run the Telegram Application (blocking until shutdown).

        ``run_polling()`` manages its own event loop internally, so this
        method is intentionally synchronous.
        """
        # Use a custom HTTPXRequest with increased timeouts to handle network instability.
        request = HTTPXRequest(
            connect_timeout=20,
            read_timeout=30,
            write_timeout=20,
            pool_timeout=20,
        )

        self._app = (
            ApplicationBuilder()
            .token(self._settings.telegram_bot_token)
            .request(request)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )

        # Listen only to the configured channel (safety guard against
        # accidental multi-channel usage).
        self._app.add_handler(
            MessageHandler(
                (filters.ChatType.CHANNEL | filters.ChatType.GROUPS)
                & filters.Chat(chat_id=self._settings.telegram_channel_id),
                channel_post_handler,
            )
        )

        logger.info(
            "Telegram bot polling started. Listening for alerts in channel %d …",
            self._settings.telegram_channel_id,
        )
        self._app.run_polling(
            drop_pending_updates=True,
            bootstrap_retries=-1,  # Keep trying on startup
            read_timeout=30,
            connect_timeout=20,
            timeout=20,  # Long polling timeout (must be < read_timeout)
        )

    def stop(self) -> None:
        """Stop the Telegram application if it is running."""
        if self._app is not None:
            self._app.stop()

    # ------------------------------------------------------------------
    # Lifecycle hooks (private)
    # ------------------------------------------------------------------

    async def _post_init(self, application) -> None:
        """Connect to external MCP servers and register error handler."""
        application.add_error_handler(self._error_handler)

        registry = get_mcp_registry()
        configs = registry.all_configs()
        if not configs:
            logger.warning("No MCP servers configured! Check mcp_servers.yml")
            return

        for cfg in configs:
            logger.info("Registering MCP server: %s", cfg.name)
            try:
                await register_external_mcp(
                    name=cfg.name,
                    command=cfg.command,
                    args=cfg.args,
                    env=cfg.env,
                    read_only=cfg.read_only,
                )
            except Exception as e:
                logger.error("Failed to register MCP server %s: %s", cfg.name, e)

    async def _post_shutdown(self, application) -> None:
        """Cleanup on shutdown."""
        await shutdown_mcp()
        store = await get_store()
        await store.close()

    @staticmethod
    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log all Telegram errors that occur inside the polling loop."""
        exc = context.error
        if isinstance(exc, NetworkError):
            logger.error("Telegram network error [%s]: %s", type(exc).__name__, exc)
        elif isinstance(exc, TelegramError):
            logger.error("Telegram API error [%s]: %s", type(exc).__name__, exc)
        else:
            logger.exception("Unexpected error in Telegram handler: %s", exc)
