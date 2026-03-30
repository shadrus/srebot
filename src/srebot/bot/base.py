"""Abstract base class for chat bot integrations."""

import logging
from abc import ABC, abstractmethod

from srebot.config import Settings

logger = logging.getLogger(__name__)


class BotIntegration(ABC):
    """
    Abstract base class for all chat platform integrations.

    Subclasses implement the specifics of a particular platform
    (Telegram, Slack, Discord, …). The bot package's entry point
    uses a registry to pick exactly one configured integration at
    startup; running multiple integrations simultaneously is not supported.

    Args:
        settings: Application-wide settings instance.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @abstractmethod
    def is_configured(self) -> bool:
        """
        Return True if all required credentials / settings are present.

        The registry calls this to decide whether an integration should
        be started. Returning False means the integration will be skipped.
        """

    @abstractmethod
    def start(self) -> None:
        """
        Start the bot and block until shutdown.

        This is the main event loop for the integration. It should only
        return after a graceful shutdown has completed.
        Implementations that need async I/O manage their own event loop
        (e.g. via ``asyncio.run()`` or a framework helper like
        ``Application.run_polling()``).
        """

    @abstractmethod
    def stop(self) -> None:
        """
        Stop the bot gracefully.

        Called when the process receives a shutdown signal. Implementations
        should release all resources (connections, tasks, etc.).
        """
