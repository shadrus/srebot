"""Entry point — sets up and runs the Telegram bot."""

import logging
import sys

from telegram.ext import ApplicationBuilder, MessageHandler, filters

from ai_health_bot.bot.handlers import channel_post_handler
from ai_health_bot.config import get_mcp_registry, get_settings
from ai_health_bot.mcp.registry import register_external_mcp, shutdown_mcp
from ai_health_bot.state.store import get_store


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        level=getattr(logging, level),
        stream=sys.stdout,
    )
    # Quieten noisy third-party libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def post_init(application) -> None:
    """Called after bot is initialized — warm up connections."""
    # Ensure Redis is reachable
    await get_store()
    logger = logging.getLogger(__name__)
    logger.info("Redis connection established")

    # Connect to all external MCP servers
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


async def post_shutdown(application) -> None:
    """Cleanup on shutdown."""
    await shutdown_mcp()
    store = await get_store()
    await store.close()


def main() -> None:
    settings = get_settings()
    _setup_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info(
        "Starting AI Health Bot (model=%s, mode=%s)",
        settings.llm_model,
        "DRY-RUN 🔇" if settings.dry_run else "LIVE 📢",
    )

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Listen only to the configured channel (safety guard against accidental multi-channel usage)
    app.add_handler(
        MessageHandler(
            (filters.ChatType.CHANNEL | filters.ChatType.GROUPS)
            & filters.Chat(chat_id=settings.telegram_channel_id),
            channel_post_handler,
        )
    )

    logger.info(
        "Bot polling started. Listening for alerts in channel %d …",
        settings.telegram_channel_id,
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
