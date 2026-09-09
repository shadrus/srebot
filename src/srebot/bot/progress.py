"""Render and publish user-visible analysis progress."""

import asyncio
import contextlib
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable

from srebot.messages import get_chat_message
from srebot.progress import ProgressEvent, ProgressPhase

_MAX_PUBLIC_STATUS_LENGTH = 100
_UNSAFE_PUBLIC_STATUS_RE = re.compile(r"https?://|www\.|[`*_#<>\[\]~]", re.IGNORECASE)
logger = logging.getLogger(__name__)


def _safe_public_status(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_PUBLIC_STATUS_LENGTH:
        return None
    if any(unicodedata.category(character) == "Cc" for character in text):
        return None
    if _UNSAFE_PUBLIC_STATUS_RE.search(text):
        return None
    return text.rstrip(". …")


def render_progress(event: ProgressEvent, language: str) -> str:
    """Render a progress event as canonical chat Markdown.

    Args:
        event: Confirmed progress transition.
        language: Configured response language.

    Returns:
        Short localized progress message.
    """
    if event.phase == ProgressPhase.TOOL_EXECUTION:
        if public_status := _safe_public_status(event.public_status):
            return f"⏳ *{public_status}…*"
        key = "progress_additional_data"
    elif event.phase == ProgressPhase.PARTIAL_RESULTS:
        key = "mcp_failure_progress"
    elif event.phase == ProgressPhase.UNAVAILABLE_RESULTS:
        key = "mcp_unavailable_progress"
    else:
        key = "progress_analyzing_results"
    return get_chat_message(key, language, "markdown")


class ProgressPublisher:
    """Best-effort, throttled publisher for one chat progress message."""

    def __init__(
        self,
        update: Callable[[str], Awaitable[None]],
        language: str,
        *,
        min_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._update = update
        self._language = language
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_text: str | None = None
        self._last_update_at: float | None = None
        self._pending_text: str | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    async def publish(self, event: ProgressEvent) -> None:
        """Publish or queue the latest distinct progress event."""
        text = render_progress(event, self._language)
        immediate = False
        async with self._lock:
            if self._closed or text in {self._last_text, self._pending_text}:
                return
            now = self._clock()
            if self._last_update_at is None or now - self._last_update_at >= self._min_interval:
                self._last_text = text
                self._last_update_at = now
                immediate = True
            else:
                self._pending_text = text
                if self._flush_task is None:
                    delay = self._min_interval - (now - self._last_update_at)
                    self._flush_task = asyncio.create_task(self._flush_pending(delay))
        if immediate:
            await self._deliver(text)

    async def close(self) -> None:
        """Discard queued progress so it cannot overwrite a final response."""
        async with self._lock:
            self._closed = True
            self._pending_text = None
            task = self._flush_task
            self._flush_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _flush_pending(self, delay: float) -> None:
        try:
            while True:
                await self._sleep(delay)
                async with self._lock:
                    if self._closed:
                        self._flush_task = None
                        return
                    text = self._pending_text
                    self._pending_text = None
                    if text is None:
                        self._flush_task = None
                        return
                    self._last_text = text
                    self._last_update_at = self._clock()
                await self._deliver(text)
                async with self._lock:
                    if self._pending_text is None:
                        self._flush_task = None
                        return
                    elapsed = self._clock() - self._last_update_at
                    delay = max(0.0, self._min_interval - elapsed)
        except asyncio.CancelledError:
            raise

    async def _deliver(self, text: str) -> None:
        try:
            await self._update(text)
        except Exception:
            logger.warning("Could not update chat progress message", exc_info=True)
