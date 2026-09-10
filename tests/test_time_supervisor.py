"""Tests for the Time Messenger background task supervisor and dispatch."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiotimebot import EventType, Propagation

from srebot.bot.time.handlers import TimeBotIdentity, register_handlers
from srebot.bot.time.supervisor import TaskSupervisor
from srebot.config import Settings


def _settings(**overrides) -> Settings:
    values = {
        "time_base_url": "https://time.example.com",
        "time_token": "time-token",
        "time_channel_id": "channel-1",
        "dry_run": False,
    }
    values.update(overrides)
    return Settings.model_construct(**values)


def _event(
    *,
    text: str = "hello",
    user_id: str = "user-1",
    channel_id: str = "channel-1",
    post_id: str = "post-1",
) -> MagicMock:
    event = MagicMock()
    event.post.id = post_id
    event.post.message = text
    event.post.user_id = user_id
    event.post.channel_id = channel_id
    event.post.root_id = ""
    return event


class _FakeRouter:
    def __init__(self) -> None:
        self.handlers: dict[object, object] = {}

    def on(self, event_type: object):
        def decorator(func):
            self.handlers[event_type] = func
            return func

        return decorator


async def test_spawn_runs_task_to_completion():
    supervisor = TaskSupervisor()
    done = asyncio.Event()

    async def work() -> None:
        done.set()

    task = supervisor.spawn(work(), name="work")
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    assert done.is_set()
    assert not supervisor.closed
    assert not supervisor._tasks


async def test_task_exception_is_logged_without_affecting_others(caplog):
    supervisor = TaskSupervisor()
    other_done = asyncio.Event()

    async def failing() -> None:
        raise RuntimeError("boom")

    async def healthy() -> None:
        other_done.set()

    failing_task = supervisor.spawn(failing(), name="failing")
    healthy_task = supervisor.spawn(healthy(), name="healthy")
    with caplog.at_level(logging.ERROR, logger="srebot.bot.time.supervisor"):
        await asyncio.wait_for(
            asyncio.gather(failing_task, healthy_task, return_exceptions=True), timeout=1
        )
        await asyncio.sleep(0)

    assert other_done.is_set()
    assert any("boom" in record.message for record in caplog.records)


async def test_aclose_cancels_active_tasks_and_awaits_cleanup():
    supervisor = TaskSupervisor()
    cancelled = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = supervisor.spawn(work(), name="work")
    await asyncio.sleep(0)
    await asyncio.wait_for(supervisor.aclose(), timeout=1)

    assert supervisor.closed
    assert cancelled.is_set()
    assert task.cancelled()
    assert not supervisor._tasks


async def test_spawn_after_close_drops_coroutine_without_running_it():
    supervisor = TaskSupervisor()
    await supervisor.aclose()

    ran = False

    async def work() -> None:
        nonlocal ran
        ran = True

    result = supervisor.spawn(work(), name="work")
    assert result is None
    await asyncio.sleep(0)
    assert not ran


async def test_posted_handler_returns_stop_and_spawns_background_task():
    router = _FakeRouter()
    client = MagicMock()
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    supervisor = TaskSupervisor()
    settings = _settings()

    with patch("srebot.bot.time.handlers.handle_posted_event", new_callable=AsyncMock) as full:
        register_handlers(router, settings, client, identity, supervisor)
        posted = router.handlers[EventType.POSTED]
        event = _event()

        result = await asyncio.wait_for(posted(event, MagicMock()), timeout=1)

    assert result is Propagation.STOP
    await asyncio.sleep(0)
    full.assert_awaited_once_with(event, client, settings, identity)
    await supervisor.aclose()


async def test_posted_handler_ignores_other_channels_and_own_posts():
    router = _FakeRouter()
    client = MagicMock()
    identity = TimeBotIdentity(user_id="bot-user", username="srebot")
    supervisor = TaskSupervisor()
    settings = _settings()

    with patch("srebot.bot.time.handlers.handle_posted_event", new_callable=AsyncMock) as full:
        register_handlers(router, settings, client, identity, supervisor)
        posted = router.handlers[EventType.POSTED]

        other_channel = await posted(_event(channel_id="other"), MagicMock())
        own_post = await posted(_event(user_id="bot-user"), MagicMock())

    assert other_channel is Propagation.STOP
    assert own_post is Propagation.STOP
    await asyncio.sleep(0)
    full.assert_not_called()
    await supervisor.aclose()


async def test_run_drains_supervisor_before_closing_application_and_resources():
    from srebot.bot.time.integration import TimeBotIntegration

    order: list[str] = []

    class RecordingSupervisor(TaskSupervisor):
        async def aclose(self) -> None:
            order.append("supervisor.aclose")
            await super().aclose()

    class FakeApplication:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeApplication:
            order.append("app.enter")
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            order.append("app.exit")
            return False

        async def run(self) -> None:
            order.append("app.run")
            raise asyncio.CancelledError()

    async def record_shutdown() -> None:
        order.append("shutdown")

    settings = _settings()
    integration = TimeBotIntegration(settings)
    integration._shutdown_resources = record_shutdown

    client = MagicMock()
    client.raw_request = AsyncMock(return_value={"id": "bot-1", "username": "srebot"})

    with (
        patch(
            "srebot.bot.time.integration.get_agent",
            MagicMock(return_value=MagicMock(refresh_strategies=AsyncMock())),
        ),
        patch.object(TimeBotIntegration, "_register_mcp_servers", AsyncMock()),
        patch("srebot.bot.time.integration.TimeClient", MagicMock(return_value=client)),
        patch("srebot.bot.time.integration.Router", MagicMock()),
        patch("srebot.bot.time.integration.Application", FakeApplication),
        patch("srebot.bot.time.integration.TaskSupervisor", RecordingSupervisor),
        patch("srebot.bot.time.integration.register_handlers", MagicMock()),
        patch(
            "srebot.bot.time.integration.discover_time_message_limit",
            AsyncMock(return_value=5000),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await integration._run()

    assert order == [
        "app.enter",
        "app.run",
        "supervisor.aclose",
        "app.exit",
        "shutdown",
    ]
