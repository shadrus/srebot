from srebot.bot.slack.integration import SlackBotIntegration
from srebot.config import Settings


def test_slack_is_configured_true():
    """Slack integration should report configured if tokens are present."""
    settings = Settings(
        slack_bot_token="xoxb-1234",
        slack_app_token="xapp-1-123",
        telegram_bot_token="",
        telegram_channel_id=0,
        saas_agent_token="",
    )
    bot = SlackBotIntegration(settings)
    assert bot.is_configured() is True


def test_slack_is_configured_false_no_tokens():
    """Slack integration should report unconfigured if tokens are missing."""
    settings = Settings(
        slack_bot_token="",
        slack_app_token="",
        telegram_bot_token="",
        telegram_channel_id=0,
        saas_agent_token="",
    )
    bot = SlackBotIntegration(settings)
    assert bot.is_configured() is False
