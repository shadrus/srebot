"""Tests for followup_reply_handler and follow-up context saving in _handle_alert_group."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.bot.shared import RejectionReason, handle_followup_question
from srebot.bot.telegram.handlers import _handle_alert_group, followup_reply_handler
from srebot.parser.alert_parser import Alert, AlertStatus

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


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
    store.check_and_set_user_cooldown = AsyncMock(return_value=False)
    store.increment_followup_turns = AsyncMock(return_value=1)
    store.get_followup_context = AsyncMock(return_value=None)
    return store


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=("<b>Analysis result</b>", "incident123"))
    agent.followup = AsyncMock(return_value=("Follow-up answer", "incident456"))
    return agent


@pytest.fixture
def mock_message():
    msg = AsyncMock()
    msg.message_id = 42
    msg.chat_id = -100
    msg.text = "What happened?"
    placeholder = AsyncMock()
    placeholder.message_id = 99
    placeholder.edit_text = AsyncMock()
    msg.reply_text = AsyncMock(return_value=placeholder)
    return msg


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.dry_run = False
    s.followup_ttl = 3600
    s.followup_max_turns = 5
    return s


@pytest.fixture
def mock_context():
    ctx = MagicMock()
    ctx.bot.id = 12345
    ctx.bot.username = "mock_bot"
    ctx.bot.first_name = "MockBot"
    ctx.bot.get_me = AsyncMock(return_value=ctx.bot)
    return ctx


# ---------------------------------------------------------------------------
# _handle_alert_group — follow-up context saving
# ---------------------------------------------------------------------------


class TestHandleAlertGroupFollowupContext:
    async def test_saves_followup_context_after_firing(
        self, mock_store, mock_agent, mock_message, mock_settings
    ):
        alert = _firing_alert()
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_store.save_followup_context.assert_called_once()
        call_args = mock_store.save_followup_context.call_args[0]
        assert call_args[0] == "fp123"
        assert isinstance(call_args[1], str)  # rca_text
        assert isinstance(call_args[2], list)  # alert_data

    async def test_registers_bot_message_after_firing(
        self, mock_store, mock_agent, mock_message, mock_settings
    ):
        alert = _firing_alert()
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_store.register_bot_message.assert_called_once_with(
            99, "fp123", incident_id="incident123"
        )

    async def test_no_followup_context_if_resolved_during_analysis(
        self, mock_store, mock_agent, mock_message, mock_settings
    ):
        alert = _firing_alert()
        # Simulate alert resolved while analyzing
        mock_store.get_status = AsyncMock(return_value="resolved")
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_store.save_followup_context.assert_not_called()

    async def test_ttl_footer_added_to_rca_message(
        self, mock_store, mock_agent, mock_message, mock_settings
    ):
        alert = _firing_alert()
        mock_settings.followup_ttl = 3600
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        placeholder = mock_message.reply_text.return_value
        edited_text = placeholder.edit_text.call_args[0][0]
        assert "1 ч." in edited_text or "1" in edited_text or "1 h." in edited_text

    async def test_ttl_footer_not_added_on_insufficient_balance_english(
        self, mock_store, mock_agent, mock_message, mock_settings
    ):
        alert = _firing_alert()
        mock_agent.analyze = AsyncMock(
            return_value=(
                "⚠️ <b>Insufficient Balance</b>\nYour token balance is empty.",
                "inc_id",
            )
        )
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        placeholder = mock_message.reply_text.return_value
        edited_text = placeholder.edit_text.call_args[0][0]
        assert "Задайте уточняющие вопросы" not in edited_text
        assert "Ask follow-up questions" not in edited_text

    async def test_ttl_footer_not_added_on_insufficient_balance_russian(
        self, mock_store, mock_agent, mock_message, mock_settings
    ):
        alert = _firing_alert()
        mock_agent.analyze = AsyncMock(
            return_value=(
                "⚠️ <b>Недостаточно баланса</b>\nВаш баланс токенов пуст.",
                "inc_id",
            )
        )
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        placeholder = mock_message.reply_text.return_value
        edited_text = placeholder.edit_text.call_args[0][0]
        assert "Задайте уточняющие вопросы" not in edited_text
        assert "Ask follow-up questions" not in edited_text


# ---------------------------------------------------------------------------
# followup_reply_handler — happy path
# ---------------------------------------------------------------------------


class TestFollowupReplyHandler:
    def _make_update(
        self, text="покажи CPU", user_id=777, reply_to_id=99, is_bot=False, bot_id=12345
    ):
        update = MagicMock()
        msg = AsyncMock()
        msg.text = text
        msg.message_id = 200

        user = MagicMock()
        user.id = user_id
        user.is_bot = is_bot
        msg.from_user = user

        replied = MagicMock()
        replied.message_id = reply_to_id
        replied.from_user = MagicMock()
        replied.from_user.id = bot_id
        msg.reply_to_message = replied

        indicator = AsyncMock()
        indicator.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=indicator)

        update.message = msg
        return update

    async def test_happy_path_calls_agent_and_edits_indicator(
        self, mock_settings, mock_store, mock_context
    ):
        update = self._make_update()
        with (
            patch(
                "srebot.bot.telegram.handlers.handle_followup_question",
                AsyncMock(return_value=("Follow-up answer", "incident456", None)),
            ) as mock_fq,
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
        ):
            await followup_reply_handler(update, mock_context)

        mock_fq.assert_called_once()
        update.message.reply_text.assert_called()

    async def test_ignored_when_no_context(self, mock_settings, mock_context):
        update = self._make_update()
        indicator = update.message.reply_text.return_value
        with (
            patch(
                "srebot.bot.telegram.handlers.handle_followup_question",
                AsyncMock(return_value=("", None, RejectionReason.NO_CONTEXT)),
            ),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await followup_reply_handler(update, mock_context)

        # Should send indicator and then delete it
        update.message.reply_text.assert_called_once()
        indicator.delete.assert_called_once()

    async def test_sends_cooldown_message(self, mock_settings, mock_context):
        update = self._make_update()
        indicator = update.message.reply_text.return_value
        with (
            patch(
                "srebot.bot.telegram.handlers.handle_followup_question",
                AsyncMock(return_value=("", None, RejectionReason.COOLDOWN)),
            ),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await followup_reply_handler(update, mock_context)

        update.message.reply_text.assert_called_once()
        indicator.edit_text.assert_called_once()
        text = indicator.edit_text.call_args[0][0]
        assert "⏳" in text or "wait" in text

    async def test_sends_limit_message(self, mock_settings, mock_context):
        update = self._make_update()
        indicator = update.message.reply_text.return_value
        with (
            patch(
                "srebot.bot.telegram.handlers.handle_followup_question",
                AsyncMock(return_value=("", None, RejectionReason.LIMIT_REACHED)),
            ),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await followup_reply_handler(update, mock_context)

        update.message.reply_text.assert_called_once()
        indicator.edit_text.assert_called_once()
        text = indicator.edit_text.call_args[0][0]
        assert "🔒" in text or "Limit" in text

    async def test_ignores_bot_messages(self, mock_settings, mock_context):
        update = self._make_update(is_bot=True)
        with patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings):
            await followup_reply_handler(update, mock_context)

        update.message.reply_text.assert_not_called()

    async def test_ignores_messages_without_reply(self, mock_settings, mock_context):
        update = MagicMock()
        msg = AsyncMock()
        msg.text = "some text"
        msg.reply_to_message = None
        update.message = msg

        with patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings):
            await followup_reply_handler(update, mock_context)

        msg.reply_text.assert_not_called()

    async def test_dry_run_logs_instead_of_sending(self, mock_settings, mock_context):
        mock_settings.dry_run = True
        update = self._make_update()
        with (
            patch(
                "srebot.bot.telegram.handlers.handle_followup_question",
                AsyncMock(return_value=("Follow-up answer", "incident456", None)),
            ),
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
        ):
            await followup_reply_handler(update, mock_context)

        update.message.reply_text.assert_not_called()

    async def test_command_with_mention(self, mock_settings, mock_context):
        update = self._make_update(text="@mock_bot /mute 1h", reply_to_id=None)
        # Make the message from a user (is_bot = False) and a mention (not necessarily a reply)
        update.message.reply_to_message = None
        update.message.chat_id = -100

        with (
            patch("srebot.bot.telegram.handlers.get_settings", return_value=mock_settings),
            patch(
                "srebot.bot.commands.handle_command",
                AsyncMock(return_value="Muted globally"),
            ) as mock_hc,
        ):
            await followup_reply_handler(update, mock_context)

        mock_hc.assert_called_once_with("/mute 1h", None, "-100", bot_username="mock_bot")
        update.message.reply_text.assert_called_once()
        args = update.message.reply_text.call_args[0][0]
        assert "Muted globally" in args


class TestHandleFollowupQuestionDirect:
    async def test_expired_context_on_reply_falls_back_to_general_query(
        self, mock_store, mock_agent, mock_settings
    ):
        mock_store.get_bot_message_context = AsyncMock(
            return_value={"fingerprint": "fp123", "incident_id": "incident123"}
        )
        mock_store.get_followup_context = AsyncMock(return_value=None)

        with (
            patch("srebot.state.store.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.shared.get_agent", return_value=mock_agent),
            patch("srebot.config.get_settings", return_value=mock_settings),
        ):
            answer, new_incident_id, rejection = await handle_followup_question(
                reply_to_id="99",
                question="What's wrong?",
                user_id="777",
                chat_id="-100",
            )

        assert rejection is None
        assert answer == "Follow-up answer"
        mock_agent.followup.assert_called_once_with(
            question="What's wrong?",
            rca_text="",
            alert_data=[],
            allowed_servers=None,
            parent_incident_id=None,
            user_name=None,
        )

    async def test_expired_context_on_mention_falls_back_to_general_query(
        self, mock_store, mock_agent, mock_settings
    ):
        mock_store.get_last_active_incident = AsyncMock(return_value="fp123")
        mock_store.get_followup_context = AsyncMock(return_value=None)

        with (
            patch("srebot.state.store.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.shared.get_agent", return_value=mock_agent),
            patch("srebot.config.get_settings", return_value=mock_settings),
        ):
            answer, new_incident_id, rejection = await handle_followup_question(
                reply_to_id=None,
                question="What's wrong?",
                user_id="777",
                chat_id="-100",
            )

        assert rejection is None
        assert answer == "Follow-up answer"
        mock_agent.followup.assert_called_once_with(
            question="What's wrong?",
            rca_text="",
            alert_data=[],
            allowed_servers=None,
            parent_incident_id=None,
            user_name=None,
        )

    async def test_active_context_on_mention_uses_context(
        self, mock_store, mock_agent, mock_settings
    ):
        mock_store.get_last_active_incident = AsyncMock(return_value="fp123")
        mock_store.get_followup_context = AsyncMock(
            return_value={
                "rca_text": "previous rca",
                "alert_data": [{"alertname": "CPUHigh"}],
                "incident_id": "incident123",
            }
        )
        mock_store.increment_followup_turns = AsyncMock(return_value=1)

        with (
            patch("srebot.state.store.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.shared.get_agent", return_value=mock_agent),
            patch("srebot.config.get_settings", return_value=mock_settings),
        ):
            answer, new_incident_id, rejection = await handle_followup_question(
                reply_to_id=None,
                question="What's wrong?",
                user_id="777",
                chat_id="-100",
            )

        assert rejection is None
        assert answer == "Follow-up answer"
        mock_agent.followup.assert_called_once_with(
            question="What's wrong?",
            rca_text="previous rca",
            alert_data=[{"alertname": "CPUHigh"}],
            allowed_servers=None,
            parent_incident_id="incident123",
            user_name=None,
        )
