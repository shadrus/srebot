"""Tests for the new follow-up methods added to AlertStore."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

from srebot.state.store import (
    _ADMIT_FOLLOWUP_SCRIPT,
    _UPDATE_FOLLOWUP_CONTEXT_SCRIPT,
    AlertStore,
    ConversationIdentity,
    FollowupAdmission,
    conversation_identity,
)


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock(return_value=True)
    r.delete = AsyncMock()
    r.ttl = AsyncMock(return_value=3600)
    r.ping = AsyncMock()
    r.aclose = AsyncMock()
    r.eval = AsyncMock(return_value=0)
    return r


@pytest.fixture
def store(mock_redis):
    return AlertStore(redis=mock_redis, ttl=86400)


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.followup_ttl = 3600
    s.followup_user_cooldown_sec = 10
    s.followup_max_turns = 5
    s.followup_user_max_turns = None
    s.effective_followup_user_max_turns = 5
    s.followup_incident_max_turns = 20
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


class TestAtomicFollowupContextUpdates:
    async def test_incident_and_rca_updates_use_atomic_context_mutation(self, store, mock_redis):
        mock_redis.eval.return_value = 1

        await store.update_followup_context_incident_id("fp1", "incident-2")
        await store.update_followup_context_rca_text("fp1", "new rca")

        assert mock_redis.eval.await_args_list[0].args == (
            _UPDATE_FOLLOWUP_CONTEXT_SCRIPT,
            1,
            "alert:followup:fp1",
            "incident_id",
            "incident-2",
        )
        assert mock_redis.eval.await_args_list[1].args == (
            _UPDATE_FOLLOWUP_CONTEXT_SCRIPT,
            1,
            "alert:followup:fp1",
            "rca_text",
            "new rca",
        )
        mock_redis.get.assert_not_awaited()
        mock_redis.set.assert_not_awaited()

    def test_context_mutation_reads_latest_document_and_preserves_turns(self):
        assert "redis.call('GET', KEYS[1])" in _UPDATE_FOLLOWUP_CONTEXT_SCRIPT
        assert "context[ARGV[1]] = ARGV[2]" in _UPDATE_FOLLOWUP_CONTEXT_SCRIPT
        assert "context['turns'] =" not in _UPDATE_FOLLOWUP_CONTEXT_SCRIPT


class TestConversationIdentity:
    def test_scopes_platform_chat_and_user(self):
        assert conversation_identity("telegram:-100", "42").key == "telegram:-100:42"
        assert conversation_identity("discord:-100", "42").key == "discord:-100:42"

    def test_scopes_same_user_in_different_chats(self):
        first = conversation_identity("slack:C1", "U1")
        second = conversation_identity("slack:C2", "U1")
        assert first.key != second.key

    def test_rejects_unnamespaced_direct_identity(self):
        with pytest.raises(ValueError, match="platform namespace"):
            ConversationIdentity("C1", "U1")

    def test_builder_uses_namespaced_fallback(self):
        assert conversation_identity(None, "U1").key == "unknown:global:U1"


class TestAtomicFollowupAdmission:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (0, FollowupAdmission.ACCEPTED),
            (1, FollowupAdmission.COOLDOWN),
            (2, FollowupAdmission.USER_LIMIT),
            (3, FollowupAdmission.INCIDENT_LIMIT),
            (4, FollowupAdmission.NO_CONTEXT),
        ],
    )
    async def test_maps_atomic_script_result(
        self,
        store,
        mock_redis,
        mock_settings,
        result,
        expected,
    ):
        mock_redis.eval.return_value = result
        identity = conversation_identity("telegram:-100", "42")

        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            admission = await store.admit_followup("fp1", identity)

        assert admission is expected
        args = mock_redis.eval.await_args.args
        assert args[1] == 3
        assert args[2:5] == (
            "alert:followup:fp1",
            "followup:cooldown:telegram:-100:42",
            "followup:turns:user:fp1:telegram:-100:42",
        )
        assert args[-4:] == (10, 5, 20, 3600)

    def test_script_reuses_legacy_total_counter_and_remaining_context_ttl(self):
        assert "cjson.decode" in _ADMIT_FOLLOWUP_SCRIPT
        assert "context['turns']" in _ADMIT_FOLLOWUP_SCRIPT
        assert "PTTL', KEYS[1]" in _ADMIT_FOLLOWUP_SCRIPT
        assert "PEXPIRE', KEYS[3], context_ttl_ms" in _ADMIT_FOLLOWUP_SCRIPT
        assert "followup:turns:incident" not in _ADMIT_FOLLOWUP_SCRIPT
        assert "EXPIRE', KEYS[3], ARGV[4]" not in _ADMIT_FOLLOWUP_SCRIPT

    async def test_new_user_limit_overrides_legacy(self, store, mock_redis, mock_settings):
        mock_settings.followup_user_max_turns = 7
        mock_settings.effective_followup_user_max_turns = 7
        identity = conversation_identity("time:C1", "U1")

        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            await store.admit_followup("fp1", identity)

        assert mock_redis.eval.await_args.args[-3] == 7

    async def test_malformed_context_wins_over_existing_cooldown(self, mock_settings):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = AlertStore(redis=redis, ttl=86400)
        identity = conversation_identity("slack:C1", "U1")
        await redis.set("alert:followup:fp1", "{malformed", ex=3600)
        await redis.set(f"followup:cooldown:{identity.key}", "1", ex=10)

        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            admission = await store.admit_followup("fp1", identity)

        assert admission is FollowupAdmission.NO_CONTEXT
        await redis.aclose()

    @pytest.mark.parametrize(
        "context",
        [
            {"alert_data": [], "turns": 0},
            {"rca_text": "RCA", "turns": 0},
            {"rca_text": 42, "alert_data": [], "turns": 0},
            {"rca_text": "RCA", "alert_data": {}, "turns": 0},
            {"rca_text": "RCA", "alert_data": [], "turns": "0"},
            {"rca_text": "RCA", "alert_data": [], "turns": -1},
            {"rca_text": "RCA", "alert_data": [], "turns": 0.5},
        ],
    )
    async def test_unusable_context_does_not_mutate_quota_state(self, mock_settings, context):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = AlertStore(redis=redis, ttl=86400)
        identity = conversation_identity("slack:C1", "U1")
        context_key = "alert:followup:fp1"
        cooldown_key = f"followup:cooldown:{identity.key}"
        user_turns_key = f"followup:turns:user:fp1:{identity.key}"
        await redis.set(context_key, json.dumps(context), ex=3600)

        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            admission = await store.admit_followup("fp1", identity)

        assert admission is FollowupAdmission.NO_CONTEXT
        assert json.loads(await redis.get(context_key)) == context
        assert await redis.exists(cooldown_key, user_turns_key) == 0
        await redis.aclose()

    @pytest.mark.parametrize(
        "raw_context",
        [
            json.dumps(
                {
                    "rca_text": "RCA",
                    "alert_data": {},
                    "turns": 0,
                    "nested": {"alert_data": []},
                }
            ),
            json.dumps(
                {
                    "rca_text": '"alert_data": [',
                    "alert_data": {},
                    "turns": 0,
                }
            ),
            '{"rca_text":"RCA","alert_data":[],"alert_data":{},"turns":0}',
        ],
    )
    async def test_top_level_array_validation_rejects_decoys_and_duplicates(
        self,
        mock_settings,
        raw_context,
    ):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = AlertStore(redis=redis, ttl=86400)
        identity = conversation_identity("slack:C1", "U1")
        context_key = "alert:followup:fp1"
        cooldown_key = f"followup:cooldown:{identity.key}"
        user_turns_key = f"followup:turns:user:fp1:{identity.key}"
        await redis.set(context_key, raw_context, ex=3600)

        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            admission = await store.admit_followup("fp1", identity)

        assert admission is FollowupAdmission.NO_CONTEXT
        assert await redis.get(context_key) == raw_context
        assert await redis.exists(cooldown_key, user_turns_key) == 0
        await redis.aclose()

    async def test_scoped_general_query_cooldown(self, store, mock_redis, mock_settings):
        identity = conversation_identity("discord:C1", "42")
        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            in_cooldown = await store.check_and_set_scoped_cooldown(identity)

        assert in_cooldown is False
        mock_redis.set.assert_awaited_with(
            "followup:cooldown:discord:C1:42",
            "1",
            ex=10,
            nx=True,
        )

    async def test_lua_concurrent_requests_cannot_exceed_final_incident_slot(
        self,
        mock_settings,
    ):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        store = AlertStore(redis=redis, ttl=86400)
        await redis.set(
            "alert:followup:fp1",
            json.dumps({"rca_text": "rca", "alert_data": [], "turns": 0}),
            ex=3600,
        )
        mock_settings.followup_user_max_turns = 5
        mock_settings.effective_followup_user_max_turns = 5
        mock_settings.followup_incident_max_turns = 1

        with patch("srebot.state.store.get_settings", return_value=mock_settings):
            results = await asyncio.gather(
                store.admit_followup("fp1", conversation_identity("slack:C1", "U1")),
                store.admit_followup("fp1", conversation_identity("slack:C1", "U2")),
            )

        assert results.count(FollowupAdmission.ACCEPTED) == 1
        assert results.count(FollowupAdmission.INCIDENT_LIMIT) == 1
        context = json.loads(await redis.get("alert:followup:fp1"))
        assert context["turns"] == 1
        await redis.aclose()

    async def test_lua_concurrent_requests_cannot_exceed_final_user_slot(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(
            "alert:followup:fp1",
            json.dumps({"rca_text": "rca", "alert_data": [], "turns": 0}),
            ex=3600,
        )
        common = (
            _ADMIT_FOLLOWUP_SCRIPT,
            3,
            "alert:followup:fp1",
        )
        results = await asyncio.gather(
            redis.eval(
                *common,
                "followup:cooldown:request-1",
                "followup:turns:user:fp1:slack:C1:U1",
                10,
                1,
                10,
                3600,
            ),
            redis.eval(
                *common,
                "followup:cooldown:request-2",
                "followup:turns:user:fp1:slack:C1:U1",
                10,
                1,
                10,
                3600,
            ),
        )

        assert sorted(results) == [0, 2]
        assert await redis.get("followup:turns:user:fp1:slack:C1:U1") == "1"
        await redis.aclose()
