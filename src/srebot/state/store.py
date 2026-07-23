"""Redis-backed alert deduplication store."""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum

import redis.asyncio as aioredis

from srebot.config import get_settings

logger = logging.getLogger(__name__)

_ADMIT_FOLLOWUP_SCRIPT = """
local function top_level_value_is_array(raw, key)
    local quoted_key = '"' .. key .. '"'
    local depth = 0
    local in_string = false
    local escaped = false
    local found = false
    local index = 1
    while index <= string.len(raw) do
        local char = string.sub(raw, index, index)
        if in_string then
            if escaped then
                escaped = false
            elseif char == '\\\\' then
                escaped = true
            elseif char == '"' then
                in_string = false
            end
        elseif char == '"' then
            if depth == 1
                and string.sub(raw, index, index + string.len(quoted_key) - 1) == quoted_key then
                local cursor = index + string.len(quoted_key)
                while string.match(string.sub(raw, cursor, cursor), '%s') do
                    cursor = cursor + 1
                end
                if string.sub(raw, cursor, cursor) == ':' then
                    if found then
                        return false
                    end
                    found = true
                    cursor = cursor + 1
                    while string.match(string.sub(raw, cursor, cursor), '%s') do
                        cursor = cursor + 1
                    end
                    if string.sub(raw, cursor, cursor) ~= '[' then
                        return false
                    end
                end
            end
            in_string = true
        elseif char == '{' or char == '[' then
            depth = depth + 1
        elseif char == '}' or char == ']' then
            depth = depth - 1
        end
        index = index + 1
    end
    return found
end

local context_raw = redis.call('GET', KEYS[1])
if not context_raw then
    return 4
end
local decoded, context = pcall(cjson.decode, context_raw)
if not decoded or type(context) ~= 'table' then
    return 4
end
if type(context['rca_text']) ~= 'string'
    or type(context['alert_data']) ~= 'table'
    or not top_level_value_is_array(context_raw, 'alert_data')
    or type(context['turns']) ~= 'number'
    or context['turns'] < 0
    or context['turns'] ~= math.floor(context['turns']) then
    return 4
end
local context_ttl_ms = redis.call('PTTL', KEYS[1])
if context_ttl_ms == 0 or context_ttl_ms == -2 then
    return 4
end
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 1
end
local user_turns = tonumber(redis.call('GET', KEYS[3]) or '0')
local incident_turns = context['turns']
if user_turns >= tonumber(ARGV[2]) then
    return 2
end
if incident_turns >= tonumber(ARGV[3]) then
    return 3
end
if context_ttl_ms < 0 then
    context_ttl_ms = tonumber(ARGV[4]) * 1000
end
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
redis.call('INCR', KEYS[3])
redis.call('PEXPIRE', KEYS[3], context_ttl_ms)
context['turns'] = incident_turns + 1
redis.call('SET', KEYS[1], cjson.encode(context), 'PX', context_ttl_ms)
return 0
"""

_UPDATE_FOLLOWUP_CONTEXT_SCRIPT = """
local context_raw = redis.call('GET', KEYS[1])
if not context_raw then
    return 0
end
local decoded, context = pcall(cjson.decode, context_raw)
if not decoded or type(context) ~= 'table' then
    return 0
end
local context_ttl_ms = redis.call('PTTL', KEYS[1])
if context_ttl_ms == 0 or context_ttl_ms == -2 then
    return 0
end
context[ARGV[1]] = ARGV[2]
if context_ttl_ms < 0 then
    redis.call('SET', KEYS[1], cjson.encode(context))
else
    redis.call('SET', KEYS[1], cjson.encode(context), 'PX', context_ttl_ms)
end
return 1
"""


def _decode_redis_value(value: str | bytes) -> str:
    """Return a Redis value as text."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _load_json_dict(value: str | bytes, context: str) -> dict | None:
    """Parse a Redis JSON object and log malformed payloads."""
    try:
        data = json.loads(_decode_redis_value(value))
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Invalid JSON in Redis for %s: %s", context, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Invalid Redis payload for %s: expected object, got %s", context, type(data))
        return None
    return data


def is_usable_followup_context(context: dict | None) -> bool:
    """Return whether a decoded follow-up context is safe for quota admission."""
    if not isinstance(context, dict):
        return False
    turns = context.get("turns")
    return (
        isinstance(context.get("rca_text"), str)
        and isinstance(context.get("alert_data"), list)
        and isinstance(turns, int)
        and not isinstance(turns, bool)
        and turns >= 0
    )


class FollowupAdmission(StrEnum):
    """Result of an atomic follow-up quota admission."""

    ACCEPTED = "accepted"
    COOLDOWN = "cooldown"
    USER_LIMIT = "user_limit"
    INCIDENT_LIMIT = "incident_limit"
    NO_CONTEXT = "no_context"


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Platform, chat, and user scope used for conversation quotas."""

    chat_id: str
    user_id: str

    def __post_init__(self) -> None:
        if ":" not in self.chat_id:
            raise ValueError("chat_id must include a platform namespace")
        if not self.user_id:
            raise ValueError("user_id must not be empty")

    @property
    def key(self) -> str:
        """Return the stable Redis key fragment."""
        return f"{self.chat_id}:{self.user_id}"


def conversation_identity(chat_id: str | None, user_id: int | str) -> ConversationIdentity:
    """Build a namespaced conversation identity with a safe fallback for direct API use."""
    normalized_chat_id = chat_id if chat_id and ":" in chat_id else "unknown:global"
    return ConversationIdentity(chat_id=normalized_chat_id, user_id=str(user_id))


class AlertStore:
    """
    Persists alert state in Redis to deduplicate repeated firings.

    Key schema: ``alert:{fingerprint}``
    Value: JSON ``{"status": "firing"|"resolved"|"analyzing", "reply_message_id": int}``
    TTL: configured via ``ALERT_FINGERPRINT_TTL`` (default 24 h).
    """

    def __init__(self, redis: aioredis.Redis, ttl: int) -> None:
        self._redis = redis
        self._ttl = ttl

    @classmethod
    async def create(cls) -> AlertStore:
        settings = get_settings()
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        return cls(client, settings.alert_fingerprint_ttl)

    def _key(self, fingerprint: str) -> str:
        return f"alert:{fingerprint}"

    async def is_new(self, fingerprint: str) -> bool:
        """Return True if this fingerprint is NOT already tracked as firing or analyzing."""
        value = await self._redis.get(self._key(fingerprint))
        if value is None:
            return True
        data = _load_json_dict(value, self._key(fingerprint))
        if data is None:
            return True
        return data.get("status") not in ("firing", "analyzing")

    async def mark_analyzing(
        self, fingerprint: str, reply_message_id: int | str | None = None
    ) -> None:
        """Mark fingerprint as under analysis."""
        await self._redis.set(
            self._key(fingerprint),
            json.dumps({"status": "analyzing", "reply_message_id": reply_message_id}),
            ex=self._ttl,
        )
        logger.debug("Marked analyzing: %s", fingerprint)

    async def mark_firing(self, fingerprint: str, reply_message_id: int | str) -> None:
        """Mark fingerprint as firing, storing the reply message ID."""
        await self._redis.set(
            self._key(fingerprint),
            json.dumps({"status": "firing", "reply_message_id": reply_message_id}),
            ex=self._ttl,
        )
        logger.debug("Marked firing: %s (reply_msg=%s)", fingerprint, reply_message_id)

    async def mark_resolved(self, fingerprint: str) -> None:
        """Remove fingerprint from store — next firing triggers fresh analysis."""
        await self._redis.delete(self._key(fingerprint))
        logger.debug("Marked resolved (deleted): %s", fingerprint)

    async def get_reply_message_id(self, fingerprint: str) -> int | str | None:
        """Return the reply message ID for an existing firing alert, or None."""
        value = await self._redis.get(self._key(fingerprint))
        if value is None:
            return None
        data = _load_json_dict(value, self._key(fingerprint))
        return data.get("reply_message_id") if data else None

    async def get_status(self, fingerprint: str) -> str | None:
        """Return current status ('firing', 'analyzing') or None (if resolved/expired)."""
        value = await self._redis.get(self._key(fingerprint))
        if value is None:
            return None
        data = _load_json_dict(value, self._key(fingerprint))
        return data.get("status") if data else None

    async def save_followup_context(
        self,
        fingerprint: str,
        rca_text: str,
        alert_data: list[dict],
        incident_id: str | None = None,
    ) -> None:
        """
        Persist the RCA text and alert data for follow-up question handling.

        Args:
            fingerprint: Alert group fingerprint.
            rca_text: The RCA analysis text shown to the engineer.
            alert_data: List of alert dicts (for tool routing in follow-up).
        """
        settings = get_settings()
        payload = json.dumps(
            {
                "rca_text": rca_text,
                "alert_data": alert_data,
                "turns": 0,
                "incident_id": incident_id,
            }
        )
        await self._redis.set(
            f"alert:followup:{fingerprint}",
            payload,
            ex=settings.followup_ttl,
        )
        logger.debug("Saved follow-up context: %s", fingerprint)

    async def get_followup_context(self, fingerprint: str) -> dict | None:
        """
        Retrieve the follow-up context for the given alert group.

        Args:
            fingerprint: Alert group fingerprint.

        Returns:
            Dict with keys ``rca_text``, ``alert_data``, ``turns``, or None if expired.
        """
        value = await self._redis.get(f"alert:followup:{fingerprint}")
        if value is None:
            return None
        return _load_json_dict(value, f"alert:followup:{fingerprint}")

    async def register_bot_message(
        self, message_id: int | str, fingerprint: str, incident_id: str | None = None
    ) -> None:
        """
        Store a reverse index from bot message ID to alert group fingerprint and incident ID.

        Used to detect when a user replies to a bot message in a conversation.

        Args:
            message_id: Message ID of the bot's reply.
            fingerprint: Alert group fingerprint to associate with this message.
            incident_id: ID of the backend incident log.
        """
        settings = get_settings()
        payload = json.dumps({"fingerprint": fingerprint, "incident_id": incident_id})
        await self._redis.set(
            f"followup:bymsid:{message_id}",
            payload,
            ex=settings.followup_ttl,
        )
        logger.debug(
            "Registered bot message %s → fp=%s, inc=%s",
            message_id,
            fingerprint,
            incident_id,
        )

    async def get_bot_message_context(self, message_id: int | str) -> dict | None:
        """
        Retrieve context (fingerprint and incident_id) by bot message ID.
        """
        value = await self._redis.get(f"followup:bymsid:{message_id}")
        if not value:
            return None
        value = _decode_redis_value(value)
        if value.startswith("{"):
            return _load_json_dict(value, f"followup:bymsid:{message_id}")
        # Fallback for legacy plain fingerprint strings in Redis
        return {"fingerprint": value, "incident_id": None}

    async def get_fp_by_message_id(self, message_id: int | str) -> str | None:
        """
        Look up the alert group fingerprint by a bot message ID.

        Args:
            message_id: Bot message ID to look up.

        Returns:
            Alert group fingerprint, or None if not found / expired.
        """
        ctx = await self.get_bot_message_context(message_id)
        return ctx["fingerprint"] if ctx else None

    async def set_last_active_incident(self, chat_id: str | int, fingerprint: str) -> None:
        """
        Store the last active incident fingerprint for a chat.
        """
        settings = get_settings()
        await self._redis.set(
            f"last_incident:{chat_id}",
            fingerprint,
            ex=settings.followup_ttl,
        )
        logger.debug("Set last active incident for chat %s to %s", chat_id, fingerprint)

    async def get_last_active_incident(self, chat_id: str | int) -> str | None:
        """
        Get the last active incident fingerprint for a chat.
        """
        val = await self._redis.get(f"last_incident:{chat_id}")
        return _decode_redis_value(val) if val is not None else None

    async def update_followup_context_incident_id(self, fingerprint: str, incident_id: str) -> None:
        """
        Update the incident_id inside the followup context for a fingerprint.
        """
        key = f"alert:followup:{fingerprint}"
        updated = await self._redis.eval(
            _UPDATE_FOLLOWUP_CONTEXT_SCRIPT,
            1,
            key,
            "incident_id",
            incident_id,
        )
        if not updated:
            return
        logger.debug(
            "Updated followup context for %s with incident_id %s",
            fingerprint,
            incident_id,
        )

    async def update_followup_context_rca_text(self, fingerprint: str, rca_text: str) -> None:
        """
        Update the rca_text inside the followup context for a fingerprint.
        Useful when an alert was triggered on-demand and we need to save the first analysis result.
        """
        key = f"alert:followup:{fingerprint}"
        updated = await self._redis.eval(
            _UPDATE_FOLLOWUP_CONTEXT_SCRIPT,
            1,
            key,
            "rca_text",
            rca_text,
        )
        if not updated:
            return
        logger.debug("Updated followup context for %s with new rca_text", fingerprint)

    async def check_and_set_scoped_cooldown(self, identity: ConversationIdentity) -> bool:
        """Atomically check and set cooldown for a platform/chat/user identity."""
        settings = get_settings()
        key = f"followup:cooldown:{identity.key}"
        was_set = await self._redis.set(
            key,
            "1",
            ex=settings.followup_user_cooldown_sec,
            nx=True,
        )
        return not was_set

    async def admit_followup(
        self,
        fingerprint: str,
        identity: ConversationIdentity,
    ) -> FollowupAdmission:
        """Atomically reserve one per-user and total incident follow-up turn.

        Args:
            fingerprint: Alert group fingerprint with active follow-up context.
            identity: Platform/chat/user scope for cooldown and per-user turns.

        Returns:
            Structured admission result.
        """
        settings = get_settings()
        keys = (
            f"alert:followup:{fingerprint}",
            f"followup:cooldown:{identity.key}",
            f"followup:turns:user:{fingerprint}:{identity.key}",
        )
        result = await self._redis.eval(
            _ADMIT_FOLLOWUP_SCRIPT,
            len(keys),
            *keys,
            settings.followup_user_cooldown_sec,
            settings.effective_followup_user_max_turns,
            settings.followup_incident_max_turns,
            settings.followup_ttl,
        )
        mapping = {
            0: FollowupAdmission.ACCEPTED,
            1: FollowupAdmission.COOLDOWN,
            2: FollowupAdmission.USER_LIMIT,
            3: FollowupAdmission.INCIDENT_LIMIT,
            4: FollowupAdmission.NO_CONTEXT,
        }
        admission = mapping.get(int(result))
        if admission is None:
            raise RuntimeError(f"Unexpected Redis follow-up admission result: {result}")
        logger.info(
            "Follow-up admission result=%s chat=%s user=%s fingerprint=%s",
            admission,
            identity.chat_id,
            identity.user_id,
            fingerprint,
        )
        return admission

    async def is_muted(self, chat_id: str, alertname: str) -> bool:
        """
        Check if alerts are muted for the given chat_id.
        Mute can be global or specific to an alertname.
        """
        # Check global mute
        global_mute = await self._redis.get(f"mute:global:{chat_id}")
        if global_mute is not None:
            return True

        # Check alert-specific mute
        alert_mute = await self._redis.get(f"mute:alert:{chat_id}:{alertname}")
        if alert_mute is not None:
            return True

        return False

    async def set_global_mute(self, chat_id: str, duration_seconds: int) -> None:
        """Set a global mute for the given chat_id for a duration."""
        await self._redis.set(f"mute:global:{chat_id}", "1", ex=duration_seconds)
        logger.info("Global mute set for chat %s for %d seconds", chat_id, duration_seconds)

    async def set_alert_mute(self, chat_id: str, alertname: str, duration_seconds: int) -> None:
        """Set an alert-specific mute for the given chat_id for a duration."""
        await self._redis.set(f"mute:alert:{chat_id}:{alertname}", "1", ex=duration_seconds)
        logger.info(
            "Alert mute set for chat %s, alert %s for %d seconds",
            chat_id,
            alertname,
            duration_seconds,
        )

    async def clear_global_mute(self, chat_id: str) -> None:
        """Clear the global mute for the given chat_id."""
        await self._redis.delete(f"mute:global:{chat_id}")
        logger.info("Global mute cleared for chat %s", chat_id)

    async def clear_alert_mute(self, chat_id: str, alertname: str) -> None:
        """Clear an alert-specific mute for the given chat_id."""
        await self._redis.delete(f"mute:alert:{chat_id}:{alertname}")
        logger.info("Alert mute cleared for chat %s, alert %s", chat_id, alertname)

    async def clear_all_mutes(self, chat_id: str) -> None:
        """Clear global mute and all alert-specific mutes for the given chat_id."""
        await self._redis.delete(f"mute:global:{chat_id}")
        # Find all alert-specific keys for this chat
        async for key in self._redis.scan_iter(match=f"mute:alert:{chat_id}:*"):
            await self._redis.delete(key)
        logger.info("All mutes cleared for chat %s", chat_id)

    async def get_active_mutes(self, chat_id: str) -> dict[str, int | None | dict[str, int]]:
        """
        Get all active mutes for the given chat_id with their remaining TTLs in seconds.
        """
        global_key = f"mute:global:{chat_id}"
        global_ttl = await self._redis.ttl(global_key)

        global_active_ttl = global_ttl if global_ttl >= 0 else None

        alerts = {}
        # Find all alert-specific keys for this chat
        async for key_raw in self._redis.scan_iter(match=f"mute:alert:{chat_id}:*"):
            key = key_raw.decode() if isinstance(key_raw, bytes) else key_raw
            ttl = await self._redis.ttl(key)
            if ttl >= 0:
                alertname = key.split(":")[-1]
                alerts[alertname] = ttl

        return {
            "global_ttl": global_active_ttl,
            "alerts": alerts,
        }

    async def ping(self) -> None:
        """Ping the underlying Redis to ensure connectivity."""
        await self._redis.ping()

    async def close(self) -> None:
        await self._redis.aclose()


# Module-level singleton
_store: AlertStore | None = None


async def get_store() -> AlertStore:
    global _store
    if _store is None:
        _store = await AlertStore.create()
    return _store
