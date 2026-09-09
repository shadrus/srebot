"""Supervisor that owns Time Messenger background analysis tasks."""

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)


class TaskSupervisor:
    """
    Own background tasks from spawn to completion.

    One task's exception is logged without terminating other tasks or the
    bot process. After ``aclose`` no new tasks are accepted; active ones are
    cancelled and awaited so shared resources close only afterwards.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether the supervisor stopped accepting new tasks."""
        return self._closed

    def spawn(self, coro: Coroutine[None, None, None], *, name: str) -> asyncio.Task[None] | None:
        """
        Start one tracked background task.

        Args:
            coro: Coroutine to run; closed without awaiting when shut down.
            name: Task name used for logging.

        Returns:
            The started task, or None when the supervisor is already closed.
        """
        if self._closed:
            logger.warning("Dropping background task %r: supervisor is closed", name)
            coro.close()
            return None
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    async def aclose(self) -> None:
        """Stop accepting tasks, cancel active ones, and await their cleanup."""
        self._closed = True
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.error("Background task %s failed: %r", task.get_name(), result)
        self._tasks.clear()

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Background task %s failed: %r", task.get_name(), exc)
