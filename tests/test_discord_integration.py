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
