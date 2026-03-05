"""Entry point — sets up and runs the Telegram bot."""

import logging
import sys

from telegram.ext import ApplicationBuilder, MessageHandler, filters

from ai_health_bot.bot.handlers import channel_post_handler
from ai_health_bot.config import get_cluster_registry, get_settings
from ai_health_bot.mcp.tools import close_http
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
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


async def post_init(application) -> None:
    """Called after bot is initialized — warm up connections."""
    # Ensure Redis is reachable
    await get_store()
    logger = logging.getLogger(__name__)
    logger.info("Redis connection established")

    # Log cluster config summary
    registry = get_cluster_registry()
    if registry.all_names():
        logger.info("Configured clusters: %s", registry.all_names())
    else:
        logger.warning("No clusters configured — tool calls will fail! Check clusters.yml")


async def post_shutdown(application) -> None:
    """Cleanup on shutdown."""
    await close_http()
    store = await get_store()
    await store.close()


def main() -> None:
    settings = get_settings()
    _setup_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info("Starting AI Health Bot (model=%s)", settings.llm_model)

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Listen to channel posts AND group messages (Alertmanager can post to either)
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL | filters.ChatType.GROUPS,
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
