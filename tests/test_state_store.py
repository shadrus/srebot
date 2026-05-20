"""Tests for Redis alert store (deduplication logic)."""

from unittest.mock import AsyncMock

import pytest

from srebot.state.store import AlertStore


@pytest.fixture
def mock_redis():
    """AsyncMock that simulates Redis get/set/delete."""
    redis = AsyncMock()
    redis._store: dict[str, str] = {}

    async def fake_get(key):
        return redis._store.get(key)

    async def fake_set(key, value, ex=None):
        redis._store[key] = value

    async def fake_delete(key):
        redis._store.pop(key, None)

    async def fake_ttl(key):
        return 3600 if key in redis._store else -2

    async def fake_scan_iter(match=None):
        import fnmatch

        for key in list(redis._store.keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    redis.get = fake_get
    redis.set = fake_set
    redis.delete = fake_delete
    redis.ttl = fake_ttl
    redis.scan_iter = fake_scan_iter
    return redis


@pytest.fixture
def store(mock_redis):
    return AlertStore(redis=mock_redis, ttl=3600)


class TestAlertStore:
    async def test_is_new_when_not_in_store(self, store):
        assert await store.is_new("abc123") is True

    async def test_is_new_false_after_mark_firing(self, store):
        await store.mark_firing("abc123", reply_message_id=42)
        assert await store.is_new("abc123") is False

    async def test_is_new_true_after_mark_resolved(self, store):
        await store.mark_firing("abc123", reply_message_id=42)
        await store.mark_resolved("abc123")
        assert await store.is_new("abc123") is True

    async def test_get_reply_message_id_returns_none_when_missing(self, store):
        assert await store.get_reply_message_id("notexist") is None

    async def test_get_reply_message_id_returns_correct_id(self, store):
        await store.mark_firing("abc123", reply_message_id=99)
        assert await store.get_reply_message_id("abc123") == 99

    async def test_mark_resolved_deletes_key(self, store, mock_redis):
        await store.mark_firing("abc123", reply_message_id=1)
        assert "alert:abc123" in mock_redis._store

        await store.mark_resolved("abc123")
        assert "alert:abc123" not in mock_redis._store

    async def test_different_fingerprints_are_independent(self, store):
        await store.mark_firing("fp-one", reply_message_id=1)

        assert await store.is_new("fp-two") is True
        assert await store.is_new("fp-one") is False

    async def test_key_format(self, store, mock_redis):
        await store.mark_firing("myfp", reply_message_id=7)
        assert "alert:myfp" in mock_redis._store


class TestAlertStoreMutes:
    async def test_global_mute(self, store):
        assert await store.is_muted("chat1", "HighCpu") is False

        await store.set_global_mute("chat1", 3600)
        assert await store.is_muted("chat1", "HighCpu") is True

        await store.clear_global_mute("chat1")
        assert await store.is_muted("chat1", "HighCpu") is False

    async def test_alert_mute(self, store):
        assert await store.is_muted("chat1", "HighCpu") is False

        await store.set_alert_mute("chat1", "HighCpu", 3600)
        assert await store.is_muted("chat1", "HighCpu") is True
        assert await store.is_muted("chat1", "LowDisk") is False

        await store.clear_alert_mute("chat1", "HighCpu")
        assert await store.is_muted("chat1", "HighCpu") is False

    async def test_get_active_mutes(self, store):
        mutes = await store.get_active_mutes("chat1")
        assert mutes == {"global_ttl": None, "alerts": {}}

        await store.set_global_mute("chat1", 3600)
        mutes = await store.get_active_mutes("chat1")
        assert mutes["global_ttl"] == 3600
        assert mutes["alerts"] == {}

        await store.set_alert_mute("chat1", "HighCpu", 1800)
        mutes = await store.get_active_mutes("chat1")
        assert mutes["global_ttl"] == 3600
        assert mutes["alerts"] == {"HighCpu": 3600}

        await store.clear_all_mutes("chat1")
        mutes = await store.get_active_mutes("chat1")
        assert mutes == {"global_ttl": None, "alerts": {}}
