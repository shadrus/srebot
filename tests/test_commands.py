"""Tests for commands.py — universal command processor and muting logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.bot.commands import (
    extract_chat_id,
    handle_command,
    is_command_message,
    parse_duration,
)


def test_parse_duration():
    # Valid durations
    assert parse_duration("1h") == 3600
    assert parse_duration("30m") == 1800
    assert parse_duration("2d") == 172800
    assert parse_duration("10s") == 10
    assert parse_duration("1h 30m") == 5400
    assert parse_duration(" 2h  45min ") == 9900
    # Default duration
    assert parse_duration("") == 3600
    assert parse_duration("   ") == 3600

    # Invalid durations
    with pytest.raises(ValueError):
        parse_duration("invalid")
    with pytest.raises(ValueError):
        parse_duration("abc")


def test_extract_chat_id():
    # Telegram Message mock
    tg_msg = MagicMock()
    # Delete 'channel' to avoid triggering the Discord check
    if hasattr(tg_msg, "channel"):
        del tg_msg.channel
    tg_msg.chat_id = 987654321
    assert extract_chat_id(tg_msg) == "telegram:987654321"

    # Discord Message mock
    discord_msg = MagicMock()
    # Delete 'chat_id' to avoid triggering the Telegram check
    if hasattr(discord_msg, "chat_id"):
        del discord_msg.chat_id
    discord_msg.channel.id = 123456789
    assert extract_chat_id(discord_msg) == "discord:123456789"

    # Slack channel_id as string
    assert extract_chat_id("C12345") == "slack:C12345"

    # Unsupported argument
    assert extract_chat_id(12345) is None
    assert extract_chat_id() is None


def test_is_command_message():
    assert is_command_message("mute") is True
    assert is_command_message("  Mute 1h ") is True
    assert is_command_message("/mute") is True
    assert is_command_message("/mute 30m") is True
    assert is_command_message("unmute") is True
    assert is_command_message("  Unmute ") is True
    assert is_command_message("/unmute") is True
    assert is_command_message("status") is True
    assert is_command_message("/status") is True
    assert is_command_message("/status@MyBot") is True

    # Not command messages
    assert is_command_message("mute why is CPU high?") is False
    assert is_command_message("hello") is False
    assert is_command_message("") is False


@pytest.mark.asyncio
async def test_handle_command_global_mute():
    mock_store = AsyncMock()
    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # Mute globally (no reply context)
        resp = await handle_command("mute 1h", reply_to_id=None, chat_id="telegram:123")
        assert "активирован на 1h" in resp or "activated for 1h" in resp
        mock_store.set_global_mute.assert_called_once_with("telegram:123", 3600)


@pytest.mark.asyncio
async def test_handle_command_global_unmute():
    mock_store = AsyncMock()
    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # Unmute globally
        resp = await handle_command("unmute", reply_to_id=None, chat_id="telegram:123")
        assert "отключен" in resp or "deactivated" in resp
        mock_store.clear_all_mutes.assert_called_once_with("telegram:123")


@pytest.mark.asyncio
async def test_handle_command_alert_mute_success():
    mock_store = AsyncMock()
    mock_store.get_fp_by_message_id.return_value = "fp123"
    mock_store.get_followup_context.return_value = {"alert_data": [{"alertname": "HighCpuUsage"}]}

    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # Mute specific alert (with reply context)
        resp = await handle_command("mute 30m", reply_to_id="msg456", chat_id="slack:C1")
        assert "HighCpuUsage" in resp
        assert "активирован на 30m" in resp or "activated for 30m" in resp
        mock_store.set_alert_mute.assert_called_once_with("slack:C1", "HighCpuUsage", 1800)


@pytest.mark.asyncio
async def test_handle_command_alert_mute_no_context():
    mock_store = AsyncMock()
    mock_store.get_fp_by_message_id.return_value = None  # No alert context found

    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        resp = await handle_command("mute 30m", reply_to_id="msg456", chat_id="slack:C1")
        assert "не удалось" in resp.lower() or "could not" in resp.lower()
        mock_store.set_alert_mute.assert_not_called()


@pytest.mark.asyncio
async def test_handle_command_alert_unmute_success():
    mock_store = AsyncMock()
    mock_store.get_fp_by_message_id.return_value = "fp123"
    mock_store.get_followup_context.return_value = {"alert_data": [{"alertname": "HighCpuUsage"}]}

    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # Unmute specific alert (with reply context)
        resp = await handle_command("unmute", reply_to_id="msg456", chat_id="slack:C1")
        assert "HighCpuUsage" in resp
        assert "отключен" in resp or "deactivated" in resp
        mock_store.clear_alert_mute.assert_called_once_with("slack:C1", "HighCpuUsage")


@pytest.mark.asyncio
async def test_handle_command_invalid_duration():
    mock_store = AsyncMock()
    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # Since invalid durations (like 'invalid') don't match the refined MUTE_RE regex,
        # handle_command will return None.
        resp = await handle_command("mute invalid", reply_to_id=None, chat_id="telegram:123")
        assert resp is None
        mock_store.set_global_mute.assert_not_called()


@pytest.mark.asyncio
async def test_handle_command_targeted():
    mock_store = AsyncMock()
    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # Targets this bot
        resp = await handle_command(
            "/mute@MyBot 1h", reply_to_id=None, chat_id="telegram:123", bot_username="MyBot"
        )
        assert resp is not None
        assert "активирован" in resp or "activated" in resp
        assert mock_store.set_global_mute.call_count == 1

        # Targets a different bot — should return None and not mute
        resp = await handle_command(
            "/mute@OtherBot 1h", reply_to_id=None, chat_id="telegram:123", bot_username="MyBot"
        )
        assert resp is None
        assert mock_store.set_global_mute.call_count == 1


@pytest.mark.asyncio
async def test_handle_command_status():
    mock_store = AsyncMock()
    with patch("srebot.bot.commands.get_store", return_value=mock_store):
        # 1. No active mutes
        mock_store.get_active_mutes.return_value = {"global_ttl": None, "alerts": {}}
        resp = await handle_command("status", reply_to_id=None, chat_id="telegram:123")
        assert "активных режимов тишины" in resp.lower() or "no active silence" in resp.lower()

        # 2. Global mute active
        mock_store.get_active_mutes.return_value = {"global_ttl": 3600, "alerts": {}}
        resp = await handle_command("status", reply_to_id=None, chat_id="telegram:123")
        assert "глобальный" in resp.lower() or "global" in resp.lower()
        assert "1h" in resp

        # 3. Alert mutes active
        mock_store.get_active_mutes.return_value = {
            "global_ttl": None,
            "alerts": {"HighCpu": 60, "LowDisk": 120},
        }
        resp = await handle_command("status", reply_to_id=None, chat_id="telegram:123")
        assert "highcpu" in resp.lower()
        assert "lowdisk" in resp.lower()
        assert "1m" in resp
        assert "2m" in resp

        # 4. Target bot check
        resp = await handle_command(
            "status@OtherBot", reply_to_id=None, chat_id="telegram:123", bot_username="MyBot"
        )
        assert resp is None
