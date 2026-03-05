"""Redis-backed alert deduplication store."""

import json
import logging

import redis.asyncio as aioredis

from ai_health_bot.config import get_settings

logger = logging.getLogger(__name__)


class AlertStore:
    """
    Persists alert state in Redis to deduplicate repeated firings.

    Key schema: ``alert:{fingerprint}``
    Value: JSON ``{"status": "firing"|"resolved", "reply_message_id": int}``
    TTL: configured via ``ALERT_FINGERPRINT_TTL`` (default 24 h).
    """

    def __init__(self, redis: aioredis.Redis, ttl: int) -> None:
        self._redis = redis
        self._ttl = ttl

    @classmethod
    async def create(cls) -> "AlertStore":
        settings = get_settings()
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        return cls(client, settings.alert_fingerprint_ttl)

    def _key(self, fingerprint: str) -> str:
        return f"alert:{fingerprint}"

    async def is_new(self, fingerprint: str) -> bool:
        """Return True if this fingerprint is NOT already tracked as firing."""
        value = await self._redis.get(self._key(fingerprint))
        if value is None:
            return True
        data = json.loads(value)
        return data.get("status") != "firing"

    async def mark_firing(self, fingerprint: str, reply_message_id: int) -> None:
        """Mark fingerprint as firing, storing the reply message ID."""
        await self._redis.set(
            self._key(fingerprint),
            json.dumps({"status": "firing", "reply_message_id": reply_message_id}),
            ex=self._ttl,
        )
        logger.debug("Marked firing: %s (reply_msg=%d)", fingerprint, reply_message_id)

    async def mark_resolved(self, fingerprint: str) -> None:
        """Remove fingerprint from store — next firing triggers fresh analysis."""
        await self._redis.delete(self._key(fingerprint))
        logger.debug("Marked resolved (deleted): %s", fingerprint)

    async def get_reply_message_id(self, fingerprint: str) -> int | None:
        """Return the reply message ID for an existing firing alert, or None."""
        value = await self._redis.get(self._key(fingerprint))
        if value is None:
            return None
        return json.loads(value).get("reply_message_id")

    async def close(self) -> None:
        await self._redis.aclose()


# Module-level singleton
_store: AlertStore | None = None


async def get_store() -> AlertStore:
    global _store
    if _store is None:
        _store = await AlertStore.create()
    return _store
