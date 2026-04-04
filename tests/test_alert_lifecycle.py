import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.constants import ParseMode

from srebot.bot.telegram.handlers import _handle_alert_group
from srebot.parser.alert_parser import Alert, AlertStatus


@pytest.fixture
def mock_store():
    store = AsyncMock()
    # Initial status is None
    store.get_status = AsyncMock(return_value=None)
    store.is_new = AsyncMock(return_value=True)
    store.get_reply_message_id = AsyncMock(return_value=None)
    return store


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value="<b>System Analysis Result</b>")
    return agent


@pytest.fixture
def mock_message():
    msg = AsyncMock()
    msg.message_id = 100
    placeholder = AsyncMock()
    placeholder.message_id = 101
    msg.reply_text = AsyncMock(return_value=placeholder)
    return msg


@pytest.mark.asyncio
async def test_concurrent_resolution_during_analysis(mock_store, mock_agent, mock_message):
    """
    Test that even if a RESOLVED message arrives during analysis:
    1. The analysis result is still posted to the chat.
    2. The store is NOT marked as firing (preserving resolved state).
    """
    fp = "test_fp_123"
    firing_alert = Alert(
        status=AlertStatus.FIRING,
        alertname="TestAlert",
        cluster="prod",
        labels={"job": "test"},
        fingerprint=fp,
    )
    resolved_alert = Alert(
        status=AlertStatus.RESOLVED,
        alertname="TestAlert",
        cluster="prod",
        labels={"job": "test"},
        fingerprint=fp,
    )

    # State tracking simulation
    state = {"status": None, "reply_id": None}

    async def mock_get_status(_):
        return state["status"]

    async def mock_mark_analyzing(_, rid):
        state["status"] = "analyzing"
        state["reply_id"] = rid

    async def mock_mark_resolved(_):
        state["status"] = None  # or "resolved"

    async def mock_get_reply_id(_):
        return state["reply_id"]

    mock_store.get_status.side_effect = mock_get_status
    mock_store.mark_analyzing.side_effect = mock_mark_analyzing
    mock_store.mark_resolved.side_effect = mock_mark_resolved
    mock_store.get_reply_message_id.side_effect = mock_get_reply_id

    # Slow analysis to allow concurrent resolution
    analyze_event = asyncio.Event()

    async def slow_analyze(_):
        await analyze_event.wait()
        return "<b>Full Analysis</b>"

    mock_agent.analyze.side_effect = slow_analyze

    # PATH TO PATCH: srebot.bot.telegram.handlers.get_store/agent/settings
    with (
        patch("srebot.bot.telegram.handlers.get_store", AsyncMock(return_value=mock_store)),
        patch("srebot.bot.telegram.handlers.get_agent", return_value=mock_agent),
        patch("srebot.bot.telegram.handlers.get_settings", return_value=MagicMock(dry_run=False)),
    ):
        # Start FIRING handler (Task A)
        task_a = asyncio.create_task(_handle_alert_group(fp, [firing_alert], mock_message))

        # Give it a moment to send placeholder and call mark_analyzing
        await asyncio.sleep(0.1)
        assert state["status"] == "analyzing"
        assert mock_message.reply_text.call_count == 1  # Placeholder sent

        # Now simulate RESOLVED message arriving (Task B)
        await _handle_alert_group(fp, [resolved_alert], mock_message)

        assert state["status"] is None  # Mark resolved cleared it
        # Resolved handler should have replied because it saw status="analyzing"
        assert mock_message.reply_text.call_count == 2

        # Now let Task A finish analysis
        analyze_event.set()
        await task_a

    # Verify that:
    # 1. UI was updated with analysis anyway
    placeholder = mock_message.reply_text.return_value
    placeholder.edit_text.assert_called_once_with("<b>Full Analysis</b>", parse_mode=ANY_PARSE_MODE)

    # 2. mark_firing was NOT called because status was not 'analyzing'
    mock_store.mark_firing.assert_not_called()

    # Verify that:
    # 1. UI was updated with analysis anyway
    placeholder = mock_message.reply_text.return_value
    placeholder.edit_text.assert_called_once_with("<b>Full Analysis</b>", parse_mode=ANY_PARSE_MODE)

    # 2. mark_firing was NOT called because status was not 'analyzing'
    mock_store.mark_firing.assert_not_called()


# Helper for any parse mode
ANY_PARSE_MODE = ParseMode.HTML
