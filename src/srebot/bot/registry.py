"""
Bot integration registry.

Keeps a map of integration name → factory function and provides
``create_bot()`` — the single entry point that inspects configured
credentials and returns exactly one ``BotIntegration`` instance.

Rules enforced at startup:
- Exactly **one** integration must have credentials configured.
- Zero configured integrations → fatal error (logged + RuntimeError).
- More than one configured integration → fatal error (logged + RuntimeError).
"""

import logging
from collections.abc import Callable

from srebot.bot.base import BotIntegration
from srebot.config import Settings

logger = logging.getLogger(__name__)

# Type alias: a callable that receives Settings and returns a BotIntegration.
IntegrationFactory = Callable[[Settings], BotIntegration]

_registry: dict[str, IntegrationFactory] = {}


def register(name: str, factory: IntegrationFactory) -> None:
    """
    Register a bot integration factory under the given name.

    Args:
        name: Unique identifier for the integration (e.g. ``"telegram"``).
        factory: Callable that accepts ``Settings`` and returns a
            ``BotIntegration`` instance.
    """
    if name in _registry:
        raise ValueError(f"Bot integration '{name}' is already registered.")
    _registry[name] = factory
    logger.debug("Registered bot integration: %s", name)


def create_bot(settings: Settings) -> BotIntegration:
    """
    Inspect all registered integrations and return the one that is configured.

    Exactly one integration must have credentials present. If none or more
    than one are configured the application logs a descriptive error and
    raises ``RuntimeError`` — the process is expected to exit.

    Args:
        settings: Application-wide settings instance.

    Returns:
        The single ``BotIntegration`` whose ``is_configured()`` returns True.

    Raises:
        RuntimeError: If zero or more than one integrations are configured.
    """
    configured: list[tuple[str, BotIntegration]] = []

    for name, factory in _registry.items():
        integration = factory(settings)
        if integration.is_configured():
            configured.append((name, integration))

    if len(configured) == 0:
        available = ", ".join(f"'{n}'" for n in _registry) or "<none>"
        logger.critical(
            "No bot integration is configured. "
            "Set the credentials for exactly one of the supported integrations: %s. "
            "The application cannot start.",
            available,
        )
        raise RuntimeError(
            f"No bot integration configured. Provide credentials for one of: {available}"
        )

    if len(configured) > 1:
        names = ", ".join(f"'{n}'" for n, _ in configured)
        logger.critical(
            "Multiple bot integrations are configured simultaneously: %s. "
            "Only one integration may be active at a time. "
            "Remove credentials for all integrations except the one you want to run.",
            names,
        )
        raise RuntimeError(
            f"Multiple bot integrations configured: {names}. Only one may be active at a time."
        )

    name, integration = configured[0]
    logger.info("Selected bot integration: %s", name)
    return integration
