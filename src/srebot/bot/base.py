"""Abstract base class for chat bot integrations."""

import logging
from abc import ABC, abstractmethod

from srebot.config import Settings, get_mcp_registry
from srebot.mcp.registry import register_external_mcp, shutdown_mcp
from srebot.state.store import get_store

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

    # ------------------------------------------------------------------
    # Shared lifecycle hooks
    # ------------------------------------------------------------------

    async def _register_mcp_servers(self) -> None:
        """Connect to all configured external MCP servers."""
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

    async def _shutdown_resources(self) -> None:
        """Shutdown MCP connections and Redis on teardown."""
        await shutdown_mcp()
        store = await get_store()
        await store.close()
