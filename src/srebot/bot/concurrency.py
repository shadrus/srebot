"""Process-wide admission limits that bound simultaneous analyses."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import srebot.config as config

logger = logging.getLogger(__name__)


class _KeyedGate:
    """One user's FIFO gate with holder and waiter bookkeeping."""

    def __init__(self, limit: int) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self.holders = 0
        self.waiters = 0

    @property
    def locked(self) -> bool:
        """Return whether acquiring the gate would block."""
        return self._semaphore.locked()

    @property
    def busy(self) -> bool:
        """Return whether the gate is held or awaited by anyone."""
        return self.holders > 0 or self.waiters > 0

    async def acquire(self) -> None:
        """Reserve one permit, counting the waiter until acquisition completes."""
        self.waiters += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.waiters -= 1
        self.holders += 1

    def release(self) -> None:
        """Free one permit held by the caller."""
        self.holders -= 1
        self._semaphore.release()


class ConcurrencyManager:
    """
    Bound simultaneous analyses with one global and one per-user FIFO limit.

    Slots are acquired user-first then global so a saturated user cannot
    head-of-line block the global queue, and released in reverse order.
    Per-user gates are created lazily and dropped once nobody holds or
    awaits them, so keyed semaphores cannot leak.
    """

    def __init__(self, max_total: int, max_per_user: int) -> None:
        if max_total < 1 or max_per_user < 1:
            raise ValueError("concurrency limits must be positive")
        self._max_per_user = max_per_user
        self._global = asyncio.Semaphore(max_total)
        self._user_gates: dict[str, _KeyedGate] = {}

    @asynccontextmanager
    async def analysis_slot(
        self,
        user_key: str | None = None,
        on_queued: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        """
        Hold one analysis slot for the duration of the context.

        Args:
            user_key: Conversation identity key; None exempts from the per-user limit.
            on_queued: Optional callback published before blocking acquisition.

        Raises:
            ValueError: If constructed with non-positive limits.
        """
        if user_key is not None and on_queued is not None:
            probe = self._user_gates.get(user_key)
            if self._global.locked() or (probe is not None and probe.locked):
                await on_queued()
        elif on_queued is not None and self._global.locked():
            await on_queued()

        gate: _KeyedGate | None = None
        if user_key is not None:
            gate = self._user_gates.setdefault(user_key, _KeyedGate(self._max_per_user))
            try:
                await gate.acquire()
            except BaseException:
                self._drop_idle_gate(user_key, gate)
                raise
        try:
            await self._global.acquire()
            try:
                yield
            finally:
                self._global.release()
        finally:
            if gate is not None:
                gate.release()
                self._drop_idle_gate(user_key, gate)

    def _drop_idle_gate(self, user_key: str, gate: _KeyedGate) -> None:
        """Remove the gate for a user when nobody holds or awaits it."""
        if not gate.busy and self._user_gates.get(user_key) is gate:
            del self._user_gates[user_key]


_manager: ConcurrencyManager | None = None


def get_concurrency_manager() -> ConcurrencyManager:
    """Return the process-wide concurrency manager, created from settings."""
    global _manager
    if _manager is None:
        settings = config.get_settings()
        _manager = ConcurrencyManager(
            settings.analysis_max_concurrency,
            settings.analysis_max_concurrency_per_user,
        )
    return _manager
