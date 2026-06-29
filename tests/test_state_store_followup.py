"""Tests for the new follow-up methods added to AlertStore."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.state.store import AlertStore


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock(return_value=True)
    r.delete = AsyncMock()
    r.ttl = AsyncMock(return_value=3600)
    r.ping = AsyncMock()
    r.aclose = AsyncMock()
    return r


@pytest.fixture
def store(mock_redis):
    return AlertStore(redis=mock_redis, ttl=86400)


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.followup_ttl = 3600
    s.followup_user_cooldown_sec = 10
    return s


# ---------------------------------------------------------------------------
# save_followup_context / get_followup_context
# ---------------------------------------------------------------------------


class TestFollowupContext:
    async def test_save_followup_context_stores_json(self, store, mock_redis, mock_settings):
        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            await store.save_followup_context("fp1", "rca text", [{"alertname": "CPUHigh"}])

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        payload = json.loads(call_args[0][1])

        assert key == "alert:followup:fp1"
        assert payload["rca_text"] == "rca text"
        assert payload["alert_data"] == [{"alertname": "CPUHigh"}]
        assert payload["turns"] == 0
        assert call_args[1]["ex"] == 3600

    async def test_get_followup_context_returns_none_when_missing(self, store, mock_redis):
        mock_redis.get.return_value = None
        result = await store.get_followup_context("fp1")
        assert result is None

    async def test_get_followup_context_returns_dict(self, store, mock_redis):
        data = {"rca_text": "rca", "alert_data": [], "turns": 2}
        mock_redis.get.return_value = json.dumps(data)
        result = await store.get_followup_context("fp1")
        assert result == data

    async def test_get_followup_context_handles_invalid_json(self, store, mock_redis):
        mock_redis.get.return_value = "not-json"
        result = await store.get_followup_context("fp1")
        assert result is None


# ---------------------------------------------------------------------------
# register_bot_message / get_fp_by_message_id
# ---------------------------------------------------------------------------


class TestBotMessageIndex:
    async def test_register_bot_message_sets_key(self, store, mock_redis, mock_settings):
        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            await store.register_bot_message(99, "fp1", incident_id="inc123")

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "followup:bymsid:99"
        assert json.loads(call_args[0][1]) == {"fingerprint": "fp1", "incident_id": "inc123"}
        assert call_args[1]["ex"] == 3600

    async def test_get_fp_by_message_id_returns_fingerprint(self, store, mock_redis):
        # New JSON format
        mock_redis.get.return_value = json.dumps({"fingerprint": "fp1", "incident_id": "inc123"})
        result = await store.get_fp_by_message_id(99)
        assert result == "fp1"

    async def test_get_fp_by_message_id_fallback_for_legacy_string(self, store, mock_redis):
        # Legacy plain string format
        mock_redis.get.return_value = "fp1"
        result = await store.get_fp_by_message_id(99)
        assert result == "fp1"

    async def test_get_fp_by_message_id_returns_none_when_missing(self, store, mock_redis):
        mock_redis.get.return_value = None
        result = await store.get_fp_by_message_id(99)
        assert result is None


# ---------------------------------------------------------------------------
# increment_followup_turns
# ---------------------------------------------------------------------------


class TestIncrementFollowupTurns:
    async def test_increment_returns_zero_when_no_context(self, store, mock_redis):
        mock_redis.get.return_value = None
        result = await store.increment_followup_turns("fp1")
        assert result == 0

    async def test_increment_increments_turns(self, store, mock_redis):
        data = {"rca_text": "rca", "alert_data": [], "turns": 1}
        mock_redis.get.return_value = json.dumps(data)
        mock_redis.ttl.return_value = 3000

        result = await store.increment_followup_turns("fp1")
        assert result == 2

        # Verify the updated payload was saved
        saved = json.loads(mock_redis.set.call_args[0][1])
        assert saved["turns"] == 2

    async def test_increment_handles_missing_turns_key(self, store, mock_redis):
        data = {"rca_text": "rca", "alert_data": []}  # no 'turns' key
        mock_redis.get.return_value = json.dumps(data)
        mock_redis.ttl.return_value = 100

        result = await store.increment_followup_turns("fp1")
        assert result == 1


# ---------------------------------------------------------------------------
# check_and_set_user_cooldown
# ---------------------------------------------------------------------------


class TestUserCooldown:
    async def test_returns_false_when_not_in_cooldown(self, store, mock_redis, mock_settings):
        # SET NX returns True when key was set (user NOT in cooldown)
        mock_redis.set.return_value = True
        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            result = await store.check_and_set_user_cooldown(123)
        assert result is False  # not in cooldown

    async def test_returns_true_when_in_cooldown(self, store, mock_redis, mock_settings):
        # SET NX returns None/False when key already exists (user IS in cooldown)
        mock_redis.set.return_value = None
        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            result = await store.check_and_set_user_cooldown(123)
        assert result is True  # in cooldown

    async def test_uses_correct_key_and_ttl(self, store, mock_redis, mock_settings):
        mock_redis.set.return_value = True
        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            await store.check_and_set_user_cooldown(42)

        call_kwargs = mock_redis.set.call_args[1]
        call_args = mock_redis.set.call_args[0]
        assert call_args[0] == "followup:cooldown:42"
        assert call_kwargs.get("ex") == 10
        assert call_kwargs.get("nx") is True
