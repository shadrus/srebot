"""Tests for Slack and Discord bot handlers, validating feature parity with Telegram."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.bot.discord.handlers import (
    _handle_alert_group as discord_handle_alert_group,
)
from srebot.bot.discord.handlers import (
    clean_mentions as discord_clean_mentions,
)
from srebot.bot.discord.handlers import (
    register_handlers as discord_register_handlers,
)
from srebot.bot.shared import RejectionReason
from srebot.bot.slack.handlers import (
    _handle_alert_group as slack_handle_alert_group,
)
from srebot.bot.slack.handlers import (
    clean_mentions as slack_clean_mentions,
)
from srebot.bot.slack.handlers import (
    register_handlers as slack_register_handlers,
)
from srebot.parser.alert_parser import Alert, AlertStatus


def _firing_alert(alertname="CPUHigh", cluster="prod", job="api-server") -> Alert:
    return Alert(
        status=AlertStatus.FIRING,
        alertname=alertname,
        cluster=cluster,
        labels={"job": job},
        annotations={"summary": "test"},
        fingerprint="fp123",
        namespace="default",
        severity="critical",
        source_url="https://prometheus.example.com/graph",
    )


def _resolved_alert() -> Alert:
    return Alert(
        status=AlertStatus.RESOLVED,
        alertname="CPUHigh",
        cluster="prod",
        labels={"job": "api-server"},
        annotations={},
        fingerprint="fp123",
        namespace="default",
        severity="critical",
        source_url="",
    )


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.is_new = AsyncMock(return_value=True)
    store.get_reply_message_id = AsyncMock(return_value=None)
    store.get_status = AsyncMock(return_value="analyzing")
    store.mark_firing = AsyncMock()
    store.mark_resolved = AsyncMock()
    store.mark_analyzing = AsyncMock()
    store.save_followup_context = AsyncMock()
    store.register_bot_message = AsyncMock()
    store.get_fp_by_message_id = AsyncMock(return_value=None)
    store.get_last_active_incident = AsyncMock(return_value=None)
    store.set_last_active_incident = AsyncMock()
    return store


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=("**Analysis result**", "incident123"))
    agent.followup = AsyncMock(return_value=("Follow-up answer", "incident456"))
    return agent


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.dry_run = False
    s.followup_ttl = 3600
    s.followup_max_turns = 5
    s.slack_channel_id = "C_SLACK"
    s.discord_channel_id = 9999
    s.llm_response_language = "English"
    return s


# ---------------------------------------------------------------------------
# Slack Handler Tests
# ---------------------------------------------------------------------------


class TestSlackHandlers:
    async def test_slack_handle_alert_group_firing(self, mock_store, mock_agent, mock_settings):
        alert = _firing_alert()
        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "12345.6789"})
        client.chat_update = AsyncMock()

        with (
            patch("srebot.bot.slack.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.slack.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.slack.handlers.get_settings", return_value=mock_settings),
        ):
            await slack_handle_alert_group("fp123", [alert], "C_SLACK", client)

        client.chat_postMessage.assert_called_once()
        client.chat_update.assert_called_once()
        update_text = client.chat_update.call_args[1]["text"]
        assert "Analysis result" in update_text
        assert "Ask follow-up questions" in update_text  # TTL footer should be appended

        mock_store.save_followup_context.assert_called_once()
        mock_store.register_bot_message.assert_called_once_with(
            "12345.6789", "fp123", incident_id="incident123"
        )
        mock_store.set_last_active_incident.assert_called_once_with("slack:C_SLACK", "fp123")

    async def test_slack_clean_mentions(self):
        text = "<@U12345> check the logs, please"
        assert slack_clean_mentions(text, "U12345") == "check the logs, please"
        assert slack_clean_mentions("hello <@U12345>", "U12345") == "hello"

    async def test_slack_message_handler_followup_happy_path(self, mock_store, mock_settings):
        app = MagicMock()
        slack_register_handlers(app, mock_settings)

        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "U_BOT"})
        client.users_info = AsyncMock(return_value={"user": {"profile": {"display_name": "Yury"}}})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1111"})
        client.chat_update = AsyncMock()

        event = {"channel": "C_SLACK", "ts": "12345.6789", "thread_ts": "2222", "user": "U_USER"}
        message = {"text": "<@U_BOT> what is the CPU status?", "user": "U_USER"}

        with (
            patch("srebot.bot.slack.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.slack.handlers.get_settings", return_value=mock_settings),
            patch(
                "srebot.bot.shared.handle_followup_question",
                AsyncMock(return_value=("Everything is fine", "incident-999", None)),
            ) as mock_fq,
        ):
            message_decorator = app.message()
            handler_func = message_decorator.call_args[0][0]
            await handler_func(event, message, client)

        mock_fq.assert_called_once_with(
            reply_to_id="2222",
            question="what is the CPU status",
            user_id="U_USER",
            chat_id="slack:C_SLACK",
            user_display_name="Yury",
        )
        client.chat_update.assert_called_once_with(
            channel="C_SLACK",
            ts="1111",
            text="Everything is fine",
        )
        mock_store.register_bot_message.assert_called_once_with(
            "1111", "general_query", incident_id="incident-999"
        )

    async def test_slack_message_handler_followup_cooldown(self, mock_store, mock_settings):
        app = MagicMock()
        slack_register_handlers(app, mock_settings)
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "U_BOT"})
        client.users_info = AsyncMock(return_value={"user": {"profile": {"display_name": "Yury"}}})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1111"})
        client.chat_update = AsyncMock()

        event = {"channel": "C_SLACK", "ts": "12345.6789", "thread_ts": "2222", "user": "U_USER"}
        message = {"text": "<@U_BOT> what is the CPU status?", "user": "U_USER"}

        with (
            patch("srebot.bot.slack.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.slack.handlers.get_settings", return_value=mock_settings),
            patch(
                "srebot.bot.shared.handle_followup_question",
                AsyncMock(return_value=("", None, RejectionReason.COOLDOWN)),
            ),
        ):
            message_decorator = app.message()
            handler_func = message_decorator.call_args[0][0]
            await handler_func(event, message, client)

        client.chat_update.assert_called_once()
        text = client.chat_update.call_args[1]["text"]
        assert "wait" in text or "Подождите" in text

    async def test_slack_message_handler_command_srebot(self, mock_store, mock_settings):
        app = MagicMock()
        slack_register_handlers(app, mock_settings)
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "U_BOT", "user": "srebot"})

        event = {"channel": "C_SLACK", "ts": "12345.6789", "thread_ts": None, "user": "U_USER"}
        message = {"text": "/mute@srebot 1h", "user": "U_USER"}

        with (
            patch("srebot.bot.slack.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.slack.handlers.get_settings", return_value=mock_settings),
            patch(
                "srebot.bot.commands.handle_command", AsyncMock(return_value="Muted globally")
            ) as mock_hc,
        ):
            message_decorator = app.message()
            handler_func = message_decorator.call_args[0][0]
            await handler_func(event, message, client)

        mock_hc.assert_called_once_with(
            "/mute@srebot 1h", None, "slack:C_SLACK", bot_username="srebot"
        )
        client.chat_postMessage.assert_called_once_with(
            channel="C_SLACK",
            text="Muted globally",
            thread_ts=None,
        )

    async def test_slack_message_handler_command_otherbot(self, mock_store, mock_settings):
        app = MagicMock()
        slack_register_handlers(app, mock_settings)
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "U_BOT", "user": "srebot"})

        event = {"channel": "C_SLACK", "ts": "12345.6789", "thread_ts": None, "user": "U_USER"}
        message = {"text": "/mute@otherbot 1h", "user": "U_USER"}

        with (
            patch("srebot.bot.slack.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.slack.handlers.get_settings", return_value=mock_settings),
            patch("srebot.bot.commands.handle_command", AsyncMock(return_value=None)) as mock_hc,
        ):
            message_decorator = app.message()
            handler_func = message_decorator.call_args[0][0]
            await handler_func(event, message, client)

        mock_hc.assert_called_once_with(
            "/mute@otherbot 1h", None, "slack:C_SLACK", bot_username="srebot"
        )
        client.chat_postMessage.assert_not_called()


# ---------------------------------------------------------------------------
# Discord Handler Tests
# ---------------------------------------------------------------------------


class TestDiscordHandlers:
    async def test_discord_handle_alert_group_firing(self, mock_store, mock_agent, mock_settings):
        alert = _firing_alert()
        message = AsyncMock()
        message.channel.id = 9999
        placeholder = AsyncMock()
        placeholder.id = 5555
        placeholder.edit = AsyncMock()
        message.reply = AsyncMock(return_value=placeholder)

        with (
            patch("srebot.bot.discord.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.discord.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.discord.handlers.get_settings", return_value=mock_settings),
        ):
            await discord_handle_alert_group("fp123", [alert], message)

        message.reply.assert_called_once()
        placeholder.edit.assert_called_once()
        update_content = placeholder.edit.call_args[1]["content"]
        assert "Analysis result" in update_content
        assert "Ask follow-up questions" in update_content

        mock_store.save_followup_context.assert_called_once()
        mock_store.register_bot_message.assert_called_once_with(
            "5555", "fp123", incident_id="incident123"
        )
        mock_store.set_last_active_incident.assert_called_once_with("discord:9999", "fp123")

    def test_discord_clean_mentions(self):
        text = "<@!123456> check logs, please"
        assert discord_clean_mentions(text, 123456, "srebot") == "check logs, please"
        assert (
            discord_clean_mentions("srebot check logs, please", 123456, "srebot")
            == "check logs, please"
        )

    async def test_discord_on_message_followup_happy_path(self, mock_store, mock_settings):
        bot = MagicMock()
        bot.user.id = 123456
        bot.user.name = "srebot"

        discord_register_handlers(bot, mock_settings)

        on_message_func = bot.event.call_args_list[0][0][0]

        message = AsyncMock()
        message.author = MagicMock()
        message.author.id = 777
        message.author.display_name = "Yury"
        message.author.name = "yury_user"
        message.channel.id = 9999
        message.content = "<@123456> check memory usage"

        bot.user.mentioned_in = MagicMock(return_value=True)
        message.mentions = [bot.user]
        message.reference = None

        indicator = AsyncMock()
        indicator.id = 8888
        indicator.edit = AsyncMock()
        message.reply = AsyncMock(return_value=indicator)

        with (
            patch("srebot.bot.discord.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.discord.handlers.get_settings", return_value=mock_settings),
            patch(
                "srebot.bot.shared.handle_followup_question",
                AsyncMock(return_value=("Memory is normal", "incident-888", None)),
            ) as mock_fq,
        ):
            await on_message_func(message)

        mock_fq.assert_called_once_with(
            reply_to_id=None,
            question="check memory usage",
            user_id="777",
            chat_id="discord:9999",
            user_display_name="Yury",
        )
        indicator.edit.assert_called_once_with(content="Memory is normal")
        mock_store.register_bot_message.assert_called_once_with(
            "8888", "general_query", incident_id="incident-888"
        )

    async def test_discord_on_message_command_srebot(self, mock_store, mock_settings):
        bot = MagicMock()
        bot.user.id = 123456
        bot.user.name = "srebot"
        discord_register_handlers(bot, mock_settings)
        on_message_func = bot.event.call_args_list[0][0][0]

        message = AsyncMock()
        message.author = MagicMock()
        message.author.id = 777
        message.channel.id = 9999
        message.content = "/mute@srebot 1h"
        message.reference = None
        if hasattr(message, "chat_id"):
            del message.chat_id

        with (
            patch("srebot.bot.discord.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.discord.handlers.get_settings", return_value=mock_settings),
            patch(
                "srebot.bot.commands.handle_command", AsyncMock(return_value="Muted globally")
            ) as mock_hc,
        ):
            await on_message_func(message)

        mock_hc.assert_called_once_with(
            "/mute@srebot 1h", None, "discord:9999", bot_username="srebot"
        )
        message.reply.assert_called_once_with("Muted globally")

    async def test_discord_on_message_command_otherbot(self, mock_store, mock_settings):
        bot = MagicMock()
        bot.user.id = 123456
        bot.user.name = "srebot"
        discord_register_handlers(bot, mock_settings)
        on_message_func = bot.event.call_args_list[0][0][0]

        message = AsyncMock()
        message.author = MagicMock()
        message.author.id = 777
        message.channel.id = 9999
        message.content = "/mute@otherbot 1h"
        message.reference = None
        if hasattr(message, "chat_id"):
            del message.chat_id

        with (
            patch("srebot.bot.discord.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.discord.handlers.get_settings", return_value=mock_settings),
            patch("srebot.bot.commands.handle_command", AsyncMock(return_value=None)) as mock_hc,
        ):
            await on_message_func(message)

        mock_hc.assert_called_once_with(
            "/mute@otherbot 1h", None, "discord:9999", bot_username="srebot"
        )
        message.reply.assert_not_called()
