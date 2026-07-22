"""Tests for the Time Messenger integration."""

from unittest.mock import ANY, AsyncMock, MagicMock, patch

from aiotimebot import Propagation

from srebot.bot.time.handlers import (
    TimeBotIdentity,
    TimeChatAdapter,
    _handle_alert_group,
    clean_mentions,
    handle_posted_event,
)
from srebot.bot.time.integration import TimeBotIntegration
from srebot.config import Settings
from srebot.parser.alert_parser import Alert, AlertStatus


def _settings(**overrides) -> Settings:
    values = {
        "time_base_url": "https://time.example.com",
        "time_token": "time-token",
        "time_channel_id": "channel-1",
        "dry_run": False,
        "followup_max_turns": 5,
        "followup_ttl": 43200,
    }
    values.update(overrides)
    return Settings.model_construct(**values)


def _event(
    *,
    text: str = "hello",
    user_id: str = "user-1",
    channel_id: str = "channel-1",
    post_id: str = "post-1",
    root_id: str = "",
) -> MagicMock:
    event = MagicMock()
    event.post.id = post_id
    event.post.message = text
    event.post.user_id = user_id
    event.post.channel_id = channel_id
    event.post.root_id = root_id
    return event


def _alert(status: AlertStatus = AlertStatus.FIRING) -> Alert:
    return Alert(
        status=status,
        alertname="CPUHigh",
        cluster="prod",
        labels={"job": "api"},
        annotations={"summary": "CPU is high"},
        fingerprint="source-fp",
        namespace="default",
        severity="critical",
        source_url="",
    )


def test_time_integration_requires_complete_configuration():
    assert TimeBotIntegration(_settings()).is_configured() is True
    assert TimeBotIntegration(_settings(time_token="")).is_configured() is False
    assert TimeBotIntegration(_settings(time_base_url="")).is_configured() is False
    assert TimeBotIntegration(_settings(time_channel_id="")).is_configured() is False


def test_clean_mentions_uses_time_username_syntax():
    assert clean_mentions("@SREBot, investigate this", "srebot") == "investigate this"
    assert clean_mentions("regular message", "srebot") == "regular message"


async def test_handler_ignores_other_channels_and_own_posts():
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    client = MagicMock()
    with patch("srebot.bot.time.handlers.process_alert_text", AsyncMock()) as process:
        result = await handle_posted_event(
            _event(channel_id="other"), client, _settings(), identity
        )
        own_result = await handle_posted_event(
            _event(user_id="bot-user"), client, _settings(), identity
        )

    assert result is Propagation.STOP
    assert own_result is Propagation.STOP
    process.assert_not_called()


async def test_top_level_post_uses_shared_alert_pipeline():
    event = _event(text="Alerts Firing")
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    client = MagicMock()
    store = AsyncMock()
    store.get_bot_message_context.return_value = None

    with (
        patch("srebot.bot.time.handlers.state_store.get_store", return_value=store),
        patch("srebot.bot.time.handlers.process_alert_text", AsyncMock()) as process,
    ):
        result = await handle_posted_event(event, client, _settings(), identity)

    assert result is Propagation.STOP
    process.assert_awaited_once_with("Alerts Firing", _handle_alert_group, event, client)


async def test_top_level_command_uses_universal_command_handler():
    event = _event(text="/status")
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    client = MagicMock()
    store = AsyncMock()
    store.get_bot_message_context.return_value = None

    with (
        patch("srebot.bot.time.handlers.state_store.get_store", return_value=store),
        patch(
            "srebot.bot.commands.handle_command",
            AsyncMock(return_value="No active silences"),
        ) as command,
        patch(
            "srebot.bot.time.handlers._send_thread_message",
            AsyncMock(return_value="reply-1"),
        ) as send,
        patch("srebot.bot.time.handlers.process_alert_text", AsyncMock()) as process,
    ):
        await handle_posted_event(event, client, _settings(), identity)

    command.assert_awaited_once_with("/status", None, "time:channel-1", bot_username="srebot")
    send.assert_awaited_once_with(client, event, "No active silences", False)
    process.assert_not_called()


async def test_thread_followup_uses_root_incident_context():
    event = _event(text="why?", root_id="alert-root")
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    client = MagicMock()
    store = AsyncMock()
    store.get_bot_message_context.return_value = {
        "fingerprint": "group-fp",
        "incident_id": "incident-1",
    }

    with (
        patch("srebot.bot.time.handlers.state_store.get_store", return_value=store),
        patch(
            "srebot.bot.time.handlers._send_thread_message",
            AsyncMock(return_value="indicator-1"),
        ),
        patch(
            "srebot.bot.time.handlers._get_user_display_name",
            AsyncMock(return_value="@engineer"),
        ),
        patch(
            "srebot.bot.time.handlers.handle_followup_question",
            AsyncMock(return_value=("answer", "incident-2", "group-fp", None)),
        ) as followup,
        patch(
            "srebot.bot.time.handlers._finish_followup",
            AsyncMock(return_value="indicator-1"),
        ),
    ):
        await handle_posted_event(event, client, _settings(), identity)

    followup.assert_awaited_once_with(
        reply_to_id="alert-root",
        question="why",
        user_id="user-1",
        chat_id="time:channel-1",
        user_display_name="@engineer",
        on_tool_failure=ANY,
    )
    assert store.register_bot_message.await_args_list == [
        (("indicator-1", "group-fp"), {"incident_id": "incident-2"}),
        (("alert-root", "group-fp"), {"incident_id": "incident-2"}),
    ]
    store.set_last_active_incident.assert_awaited_once_with("time:channel-1", "group-fp")


async def test_time_mcp_failure_updates_indicator():
    event = _event(text="why?", root_id="alert-root")
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    client = MagicMock()
    store = AsyncMock()
    store.get_bot_message_context.return_value = {
        "fingerprint": "group-fp",
        "incident_id": "incident-1",
    }

    async def followup_with_failure(**kwargs):
        await kwargs["on_tool_failure"](["unavailable-tool"])
        return "Partial answer", None, "group-fp", None

    with (
        patch("srebot.bot.time.handlers.state_store.get_store", return_value=store),
        patch(
            "srebot.bot.time.handlers._send_thread_message",
            AsyncMock(return_value="indicator-1"),
        ),
        patch(
            "srebot.bot.time.handlers._get_user_display_name",
            AsyncMock(return_value="@engineer"),
        ),
        patch(
            "srebot.bot.time.handlers.handle_followup_question",
            AsyncMock(side_effect=followup_with_failure),
        ),
        patch("srebot.bot.time.handlers._edit_post", AsyncMock()) as edit_post,
        patch(
            "srebot.bot.time.handlers._finish_followup",
            AsyncMock(return_value="indicator-1"),
        ),
    ):
        await handle_posted_event(event, client, _settings(), identity)

    assert "sources are unavailable" in edit_post.await_args.args[2]


async def test_direct_mention_starts_general_followup():
    event = _event(text="@srebot check the cluster")
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    client = MagicMock()
    store = AsyncMock()
    store.get_bot_message_context.return_value = None

    with (
        patch("srebot.bot.time.handlers.state_store.get_store", return_value=store),
        patch(
            "srebot.bot.time.handlers._send_thread_message",
            AsyncMock(return_value="indicator-1"),
        ),
        patch(
            "srebot.bot.time.handlers._get_user_display_name",
            AsyncMock(return_value=None),
        ),
        patch(
            "srebot.bot.time.handlers.handle_followup_question",
            AsyncMock(return_value=("answer", "incident-general", "general_query", None)),
        ) as followup,
        patch(
            "srebot.bot.time.handlers._finish_followup",
            AsyncMock(return_value="indicator-1"),
        ),
    ):
        await handle_posted_event(event, client, _settings(), identity)

    followup.assert_awaited_once_with(
        reply_to_id=None,
        question="check the cluster",
        user_id="user-1",
        chat_id="time:channel-1",
        user_display_name=None,
        on_tool_failure=ANY,
    )
    assert store.register_bot_message.await_args_list == [
        (("indicator-1", "general_query"), {"incident_id": "incident-general"}),
        (("post-1", "general_query"), {"incident_id": "incident-general"}),
    ]


async def test_adapter_creates_incident_root_and_edits_placeholder():
    event = _event(post_id="alert-root")
    client = MagicMock()
    sent_post = MagicMock(id="placeholder-1")
    client.send_message = AsyncMock(return_value=sent_post)
    adapter = TimeChatAdapter(event, client, dry_run=False)

    placeholder_id = await adapter.send_analyzing_placeholder("group-fp", "CPUHigh", _alert(), 1)
    with patch("srebot.bot.time.handlers._edit_post", AsyncMock()) as edit:
        result = await adapter.update_with_analysis(
            "group-fp", placeholder_id, "analysis", is_billing_error=True
        )

    assert placeholder_id == "placeholder-1"
    assert result == "placeholder-1"
    client.send_message.assert_awaited_once()
    assert "root_id" not in client.send_message.await_args.kwargs
    edit.assert_awaited_once_with(client, "placeholder-1", "analysis")


async def test_resolved_alert_replies_to_original_incident_root():
    event = _event(post_id="resolved-source")
    client = MagicMock()
    client.send_message = AsyncMock(return_value=MagicMock(id="resolved-reply"))
    adapter = TimeChatAdapter(event, client, dry_run=False)

    await adapter.send_resolved(
        "CPUHigh",
        _alert(AlertStatus.RESOLVED),
        current_status="firing",
        reply_to_id="analysis-root",
    )

    assert client.send_message.await_args.kwargs["root_id"] == "analysis-root"


async def test_alert_group_delegates_to_shared_workflow():
    event = _event(post_id="alert-root")
    client = MagicMock()
    with (
        patch("srebot.bot.time.handlers.execute_alert_group_workflow", AsyncMock()) as workflow,
        patch(
            "srebot.bot.time.handlers.config.get_settings",
            return_value=_settings(),
        ),
    ):
        await _handle_alert_group("group-fp", [_alert()], event, client)

    workflow.assert_awaited_once()
