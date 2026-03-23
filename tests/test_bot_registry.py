"""Tests for the bot integration registry."""

import pytest

from ai_observability_bot.bot.base import BotIntegration
from ai_observability_bot.bot.registry import create_bot, register
from ai_observability_bot.config import Settings

# ---------------------------------------------------------------------------
# Minimal stub integration for testing
# ---------------------------------------------------------------------------


class _StubIntegration(BotIntegration):
    def __init__(self, settings: Settings, *, configured: bool) -> None:
        super().__init__(settings)
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured

    def start(self) -> None:  # pragma: no cover
        pass

    def stop(self) -> None:  # pragma: no cover
        pass


def _stub_factory(configured: bool):
    """Return a factory that creates a _StubIntegration with the given configured flag."""

    def factory(settings: Settings) -> _StubIntegration:
        return _StubIntegration(settings, configured=configured)

    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    """Create a Settings instance without reading config files or env."""
    return Settings.model_construct(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_telegram_configured_returns_telegram_integration():
    """When telegram_bot_token is set, create_bot returns TelegramBotIntegration."""
    # Import here to trigger registration of the built-in telegram integration.
    import ai_observability_bot.bot  # noqa: F401
    from ai_observability_bot.bot.telegram import TelegramBotIntegration

    settings = _make_settings(telegram_bot_token="fake-token", telegram_channel_id=-123)
    bot = create_bot(settings)
    assert isinstance(bot, TelegramBotIntegration)


def test_no_integration_configured_raises():
    """When no integration has credentials, create_bot raises RuntimeError."""

    import ai_observability_bot.bot.registry as reg_module

    # Temporarily replace the registry with a clean one that has only
    # a single unconfigured stub.
    original_registry = reg_module._registry.copy()
    reg_module._registry.clear()
    reg_module._registry["stub"] = _stub_factory(configured=False)

    try:
        settings = _make_settings(telegram_bot_token="")
        with pytest.raises(RuntimeError, match="No bot integration configured"):
            create_bot(settings)
    finally:
        reg_module._registry.clear()
        reg_module._registry.update(original_registry)


def test_multiple_integrations_configured_raises():
    """When more than one integration has credentials, create_bot raises RuntimeError."""
    import ai_observability_bot.bot.registry as reg_module

    original_registry = reg_module._registry.copy()
    reg_module._registry.clear()
    reg_module._registry["alpha"] = _stub_factory(configured=True)
    reg_module._registry["beta"] = _stub_factory(configured=True)

    try:
        settings = _make_settings(telegram_bot_token="")
        with pytest.raises(RuntimeError, match="Multiple bot integrations configured"):
            create_bot(settings)
    finally:
        reg_module._registry.clear()
        reg_module._registry.update(original_registry)


def test_register_duplicate_raises():
    """Registering the same integration name twice raises ValueError."""
    import ai_observability_bot.bot.registry as reg_module

    original_registry = reg_module._registry.copy()
    reg_module._registry.clear()
    reg_module._registry["dupe"] = _stub_factory(configured=False)

    try:
        with pytest.raises(ValueError, match="already registered"):
            register("dupe", _stub_factory(configured=False))
    finally:
        reg_module._registry.clear()
        reg_module._registry.update(original_registry)
