"""Tests for telegram/handlers.py — alert group processing and channel post handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.bot.telegram.handlers import _handle_alert_group, channel_post_handler
from srebot.parser.alert_parser import Alert, AlertStatus

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

FIRING_TEXT = (
    "Alerts Firing:\n"
    "Labels:\n"
    " - alertname = CPUHigh\n"
    " - cluster = prod\n"
    " - job = api-server\n"
    " - severity = critical\n"
    " - namespace = default\n"
    "Annotations:\n"
    " - summary = CPU is too high\n"
    "Source: https://prometheus.example.com/graph\n"
)

RESOLVED_TEXT = (
    "Alerts Resolved:\n"
    "Labels:\n"
    " - alertname = CPUHigh\n"
    " - cluster = prod\n"
    " - job = api-server\n"
    " - severity = critical\n"
    " - namespace = default\n"
    "Annotations:\n"
    " - summary = CPU is too high\n"
    "Source: https://prometheus.example.com/graph\n"
)


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
    store.get_status = AsyncMock(return_value=None)
    store.mark_firing = AsyncMock()
    store.mark_resolved = AsyncMock()
    store.mark_analyzing = AsyncMock()
    return store


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value="<b>Analysis result</b>")
    return agent


@pytest.fixture
def mock_message():
    """Minimal telegram.Message stub."""
    msg = AsyncMock()
    msg.message_id = 42
    msg.chat_id = -100
    msg.text = FIRING_TEXT
    placeholder = AsyncMock()
    placeholder.message_id = 99
    placeholder.edit_text = AsyncMock()
    msg.reply_text = AsyncMock(return_value=placeholder)
    return msg


# ---------------------------------------------------------------------------
# _handle_alert_group — RESOLVED path
# ---------------------------------------------------------------------------


class TestHandleAlertGroupResolved:
    async def test_resolved_marks_store(self, mock_store, mock_agent, mock_message):
        alert = _resolved_alert()
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_store.mark_resolved.assert_called_once_with("fp123")
        mock_agent.analyze.assert_not_called()

    async def test_resolved_sends_reply_when_previously_tracked(
        self, mock_store, mock_agent, mock_message
    ):
        alert = _resolved_alert()
        mock_store.get_reply_message_id = AsyncMock(return_value=99)
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_message.reply_text.assert_called_once()
        args = mock_message.reply_text.call_args[0][0]
        assert "Resolved" in args

    async def test_resolved_no_reply_when_not_tracked(self, mock_store, mock_agent, mock_message):
        alert = _resolved_alert()
        mock_store.get_reply_message_id = AsyncMock(return_value=None)
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_alert_group — FIRING path
# ---------------------------------------------------------------------------


class TestHandleAlertGroupFiring:
    async def test_new_firing_sends_placeholder_and_edits(
        self, mock_store, mock_agent, mock_message
    ):
        alert = _firing_alert()
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        # placeholder sent
        mock_message.reply_text.assert_called_once()
        placeholder = mock_message.reply_text.return_value
        # then edited with analysis
        placeholder.edit_text.assert_called_once()
        edited_text = placeholder.edit_text.call_args[0][0]
        assert "Analysis result" in edited_text

    async def test_new_firing_marks_store_with_placeholder_id(
        self, mock_store, mock_agent, mock_message
    ):
        alert = _firing_alert()
        mock_message.reply_text.return_value.message_id = 77
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            mock_store.get_status.return_value = "analyzing"
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_store.mark_firing.assert_called_once_with("fp123", 77)

    async def test_duplicate_firing_skips_analysis(self, mock_store, mock_agent, mock_message):
        alert = _firing_alert()
        mock_store.is_new = AsyncMock(return_value=False)
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_agent.analyze.assert_not_called()
        mock_message.reply_text.assert_not_called()

    async def test_llm_failure_sends_error_message(self, mock_store, mock_agent, mock_message):
        alert = _firing_alert()
        mock_agent.analyze = AsyncMock(side_effect=RuntimeError("LLM down"))
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        placeholder = mock_message.reply_text.return_value
        edited_text = placeholder.edit_text.call_args[0][0]
        assert "Analysis failed" in edited_text

    async def test_edit_failure_falls_back_to_new_message(
        self, mock_store, mock_agent, mock_message
    ):
        alert = _firing_alert()
        placeholder = mock_message.reply_text.return_value
        placeholder.edit_text = AsyncMock(side_effect=Exception("edit failed"))
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        # reply_text called twice: placeholder + fallback
        assert mock_message.reply_text.call_count == 2


# ---------------------------------------------------------------------------
# _handle_alert_group — DRY-RUN
# ---------------------------------------------------------------------------


class TestHandleAlertGroupDryRun:
    async def test_dry_run_does_not_call_reply_text(self, mock_store, mock_agent, mock_message):
        alert = _firing_alert()
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=True)
            ),  # noqa: E501
        ):
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_message.reply_text.assert_not_called()

    async def test_dry_run_marks_firing_with_zero(self, mock_store, mock_agent, mock_message):
        alert = _firing_alert()
        with (
            patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
            patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
            patch(
                "srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=True)
            ),  # noqa: E501
        ):
            mock_store.get_status.return_value = "analyzing"
            await _handle_alert_group("fp123", [alert], mock_message)

        mock_store.mark_firing.assert_called_once_with("fp123", reply_message_id=0)


# ---------------------------------------------------------------------------
# channel_post_handler
# ---------------------------------------------------------------------------


class TestChannelPostHandler:
    async def test_non_alert_message_is_ignored(self):
        update = MagicMock()
        update.channel_post = MagicMock()
        update.channel_post.text = "Just a regular chat message"
        update.channel_post.message_id = 1
        update.channel_post.chat_id = -100

        with patch("srebot.bot.telegram.handlers.process_alert_text", AsyncMock()) as mock_proc:
            await channel_post_handler(update, MagicMock())
            mock_proc.assert_called_once_with(
                "Just a regular chat message",
                _handle_alert_group,
                update.channel_post,
            )

    async def test_no_message_returns_early(self):
        update = MagicMock()
        update.channel_post = None
        update.message = None

        with patch("srebot.bot.telegram.handlers.process_alert_text", AsyncMock()) as mock_proc:
            await channel_post_handler(update, MagicMock())
            mock_proc.assert_not_called()

    async def test_message_without_text_returns_early(self):
        update = MagicMock()
        update.channel_post = MagicMock()
        update.channel_post.text = None

        with patch("srebot.bot.telegram.handlers.process_alert_text", AsyncMock()) as mock_proc:
            await channel_post_handler(update, MagicMock())
            mock_proc.assert_not_called()

    async def test_alert_message_delegates_to_process_alert_text(self):
        update = MagicMock()
        update.channel_post = MagicMock()
        update.channel_post.text = FIRING_TEXT
        update.channel_post.message_id = 1
        update.channel_post.chat_id = -100

        with patch("srebot.bot.telegram.handlers.process_alert_text", AsyncMock()) as mock_proc:
            await channel_post_handler(update, MagicMock())
            mock_proc.assert_called_once_with(FIRING_TEXT, _handle_alert_group, update.channel_post)
