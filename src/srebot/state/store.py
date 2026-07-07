"""Redis-backed alert deduplication store."""

import json
import logging

import redis.asyncio as aioredis

from srebot.config import get_settings

logger = logging.getLogger(__name__)


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
        value = await self._redis.get(key)
        if not value:
            return
        data = _load_json_dict(value, key)
        if data is None:
            return
        data["incident_id"] = incident_id
        ttl = await self._redis.ttl(key)
        await self._redis.set(key, json.dumps(data), ex=max(ttl, 1))
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
        value = await self._redis.get(key)
        if not value:
            return
        data = _load_json_dict(value, key)
        if data is None:
            return
        data["rca_text"] = rca_text
        ttl = await self._redis.ttl(key)
        await self._redis.set(key, json.dumps(data), ex=max(ttl, 1))
        logger.debug("Updated followup context for %s with new rca_text", fingerprint)

    async def increment_followup_turns(self, fingerprint: str) -> int:
        """
        Atomically increment the follow-up turn counter for an alert group.

        Args:
            fingerprint: Alert group fingerprint.

        Returns:
            New turn count after increment. Returns 0 if context no longer exists.
        """
        key = f"alert:followup:{fingerprint}"
        value = await self._redis.get(key)
        if value is None:
            return 0
        data = _load_json_dict(value, key)
        if data is None:
            return 0
        data["turns"] = data.get("turns", 0) + 1
        ttl = await self._redis.ttl(key)
        await self._redis.set(key, json.dumps(data), ex=max(ttl, 1))
        return data["turns"]

    async def check_and_set_user_cooldown(self, user_id: int | str) -> bool:
        """
        Check if a user is in cooldown; set the cooldown key if not.

        Uses Redis SET NX to atomically check-and-set in one operation.

        Args:
            user_id: Platform user identifier.

        Returns:
            True if the user IS in cooldown (request should be throttled),
            False if the cooldown was just set (request is allowed).
        """
        settings = get_settings()
        key = f"followup:cooldown:{user_id}"
        # SET NX returns True if key was set (not in cooldown), False if already existed
        was_set = await self._redis.set(key, "1", ex=settings.followup_user_cooldown_sec, nx=True)
        return not was_set  # True means "in cooldown"

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
