from unittest.mock import AsyncMock, MagicMock, patch

from srebot.bot.discord.integration import DiscordBotIntegration
from srebot.config import Settings


def test_discord_is_configured_true():
    """Discord integration should report configured if token is present."""
    settings = Settings(
        discord_bot_token="fake-discord-token",
        discord_channel_id=12345,
        telegram_bot_token="",
        telegram_channel_id=0,
        saas_agent_token="",
    )
    bot = DiscordBotIntegration(settings)
    assert bot.is_configured() is True


def test_discord_is_configured_false_no_token():
    """Discord integration should report unconfigured if token is missing."""
    settings = Settings(
        discord_bot_token="",
        discord_channel_id=0,
        telegram_bot_token="",
        telegram_channel_id=0,
        saas_agent_token="",
    )
    bot = DiscordBotIntegration(settings)
    assert bot.is_configured() is False


async def test_discord_reconnect_does_not_repeat_shared_initialization():
    settings = Settings(discord_bot_token="fake-discord-token", discord_channel_id=12345)
    integration = DiscordBotIntegration(settings)
    fake_bot = MagicMock()
    fake_bot.user.id = 42
    fake_bot.user.__str__.return_value = "srebot"
    handlers = {}

    def capture_event(handler):
        handlers[handler.__name__] = handler
        return handler

    async def start_bot(token):
        assert token == "fake-discord-token"
        await handlers["on_ready"]()
        await handlers["on_ready"]()

    fake_bot.event.side_effect = capture_event
    fake_bot.start.side_effect = start_bot
    fake_bot.__aenter__ = AsyncMock(return_value=fake_bot)
    fake_bot.__aexit__ = AsyncMock(return_value=None)

    agent = MagicMock()
    agent.refresh_strategies = AsyncMock()
    integration._register_mcp_servers = AsyncMock()
    integration._shutdown_resources = AsyncMock()

    with (
        patch("srebot.bot.discord.integration.commands.Bot", return_value=fake_bot),
        patch("srebot.bot.discord.integration.register_handlers"),
        patch("srebot.bot.discord.integration.get_agent", return_value=agent),
    ):
        await integration._run()

    agent.refresh_strategies.assert_awaited_once()
    integration._register_mcp_servers.assert_awaited_once()
