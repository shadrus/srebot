"""Entry point — resolves the active bot integration and runs it."""

import asyncio
import logging
import sys

# Importing the bot package triggers registration of all built-in integrations.
import srebot.bot  # noqa: F401
from srebot.bot.health import start_health_server
from srebot.bot.registry import create_bot
from srebot.config import get_settings
from srebot.state.store import get_store


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


async def _startup() -> None:
    """Warm up shared infrastructure before handing off to the integration."""
    await start_health_server()
    await get_store()
    logging.getLogger(__name__).info("Redis connection established")


def main() -> None:
    settings = get_settings()
    _setup_logging(settings.log_level)

    logger = logging.getLogger(__name__)
    logger.info(
        "Starting AI Observability Bot (on %s, mode=%s)",
        settings.saas_ws_url,
        "DRY-RUN 🔇" if settings.dry_run else "LIVE 📢",
    )

    try:
        bot = create_bot(settings)
    except RuntimeError as exc:
        logger.critical("Bot startup failed: %s", exc)
        sys.exit(1)

    # Run shared infrastructure (health server + Redis) before starting the bot.
    # run_until_complete keeps the loop alive for the blocking bot.start() call.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_startup())
        bot.start()
    except Exception as exc:
        logger.critical(
            "Bot integration crashed during startup or polling: %s: %s",
            type(exc).__name__,
            exc,
        )
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
