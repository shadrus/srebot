"""Tests for the process-wide analysis concurrency manager."""

import asyncio

import pytest

from srebot.bot.concurrency import ConcurrencyManager


def test_constructor_rejects_non_positive_limits():
    with pytest.raises(ValueError):
        ConcurrencyManager(max_total=0, max_per_user=1)
    with pytest.raises(ValueError):
        ConcurrencyManager(max_total=1, max_per_user=0)
    with pytest.raises(ValueError):
        ConcurrencyManager(max_total=-1, max_per_user=1)
    with pytest.raises(ValueError):
        ConcurrencyManager(max_total=1, max_per_user=-1)


async def test_independent_users_run_in_parallel():
    manager = ConcurrencyManager(max_total=20, max_per_user=1)
    done = []

    async def holder(user: str) -> None:
        async with manager.analysis_slot(user_key=user):
            await asyncio.sleep(0.05)
            done.append(user)

    await asyncio.wait_for(asyncio.gather(holder("user-a"), holder("user-b")), timeout=1)
    assert sorted(done) == ["user-a", "user-b"]


async def test_per_user_limit_blocks_second_analysis():
    manager = ConcurrencyManager(max_total=20, max_per_user=1)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def first() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            entered.set()
            await release.wait()

    first_task = asyncio.create_task(first())
    await entered.wait()

    started = asyncio.Event()

    async def second() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            started.set()

    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert not started.is_set()

    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1)
    assert started.is_set()


async def test_global_limit_enforced_across_users():
    manager = ConcurrencyManager(max_total=1, max_per_user=3)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def first() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            entered.set()
            await release.wait()

    first_task = asyncio.create_task(first())
    await entered.wait()

    started = asyncio.Event()

    async def second() -> None:
        async with manager.analysis_slot(user_key="user-b"):
            started.set()

    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert not started.is_set()

    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1)
    assert started.is_set()


async def test_alerts_without_user_key_are_exempt_from_per_user_limit():
    manager = ConcurrencyManager(max_total=20, max_per_user=1)
    done = []

    async def alert(index: int) -> None:
        async with manager.analysis_slot():
            await asyncio.sleep(0.02)
            done.append(index)

    await asyncio.wait_for(asyncio.gather(*(alert(i) for i in range(3))), timeout=1)
    assert sorted(done) == [0, 1, 2]
    assert manager._user_gates == {}


async def test_waiters_are_admitted_in_fifo_order():
    manager = ConcurrencyManager(max_total=1, max_per_user=20)
    order: list[str] = []
    release = asyncio.Event()

    async def analysis(name: str, *, holder: bool = False) -> None:
        async with manager.analysis_slot(user_key=name):
            if holder:
                await release.wait()
            order.append(name)

    holder_task = asyncio.create_task(analysis("holder", holder=True))
    await asyncio.sleep(0)

    waiters = [asyncio.create_task(analysis(f"waiter-{i}")) for i in range(3)]
    await asyncio.sleep(0.05)
    assert order == []

    release.set()
    await asyncio.wait_for(asyncio.gather(holder_task, *waiters), timeout=1)
    assert order == ["holder", "waiter-0", "waiter-1", "waiter-2"]


async def test_exception_releases_slot():
    manager = ConcurrencyManager(max_total=1, max_per_user=1)

    with pytest.raises(RuntimeError, match="boom"):
        async with manager.analysis_slot(user_key="user-a"):
            raise RuntimeError("boom")

    started = asyncio.Event()

    async def next_analysis() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            started.set()

    await asyncio.wait_for(next_analysis(), timeout=1)
    assert started.is_set()


async def test_cancelled_holder_releases_both_slots():
    manager = ConcurrencyManager(max_total=1, max_per_user=1)
    entered = asyncio.Event()

    async def holder() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(holder())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    started = asyncio.Event()

    async def next_analysis() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            started.set()

    await asyncio.wait_for(next_analysis(), timeout=1)
    assert started.is_set()
    assert manager._user_gates == {}


async def test_cancelled_waiter_releases_gate_and_does_not_leak():
    manager = ConcurrencyManager(max_total=20, max_per_user=1)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holder() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            entered.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await entered.wait()

    async def waiter() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            pass

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert "user-a" in manager._user_gates

    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    release.set()
    await asyncio.wait_for(holder_task, timeout=1)
    assert manager._user_gates == {}


async def test_no_gate_leak_after_serial_uses():
    manager = ConcurrencyManager(max_total=20, max_per_user=3)

    for _ in range(3):
        async with manager.analysis_slot(user_key="user-a"):
            pass

    assert manager._user_gates == {}


async def test_on_queued_not_called_when_slots_are_free():
    manager = ConcurrencyManager(max_total=5, max_per_user=2)
    calls: list[str] = []

    async def on_queued() -> None:
        calls.append("queued")

    async with manager.analysis_slot(user_key="user-a", on_queued=on_queued):
        pass

    assert calls == []


async def test_on_queued_called_when_global_limit_is_busy():
    manager = ConcurrencyManager(max_total=1, max_per_user=3)
    calls: list[str] = []
    release = asyncio.Event()

    async def on_queued() -> None:
        calls.append("queued")

    async def holder() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            await release.wait()

    async def waiter() -> None:
        async with manager.analysis_slot(user_key="user-b", on_queued=on_queued):
            calls.append("admitted")

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert calls == ["queued"]

    release.set()
    await asyncio.wait_for(asyncio.gather(holder_task, waiter_task), timeout=1)
    assert calls == ["queued", "admitted"]


async def test_on_queued_called_when_user_limit_is_busy():
    manager = ConcurrencyManager(max_total=5, max_per_user=1)
    calls: list[str] = []
    release = asyncio.Event()

    async def on_queued() -> None:
        calls.append("queued")

    async def holder() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            await release.wait()

    async def waiter() -> None:
        async with manager.analysis_slot(user_key="user-a", on_queued=on_queued):
            calls.append("admitted")

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert calls == ["queued"]

    release.set()
    await asyncio.wait_for(asyncio.gather(holder_task, waiter_task), timeout=1)
    assert calls == ["queued", "admitted"]


async def test_on_queued_called_for_exempt_alert_when_global_is_busy():
    manager = ConcurrencyManager(max_total=1, max_per_user=1)
    calls: list[str] = []
    release = asyncio.Event()

    async def on_queued() -> None:
        calls.append("queued")

    async def holder() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            await release.wait()

    async def alert() -> None:
        async with manager.analysis_slot(on_queued=on_queued):
            calls.append("alert-admitted")

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    alert_task = asyncio.create_task(alert())
    await asyncio.sleep(0.05)
    assert calls == ["queued"]

    release.set()
    await asyncio.wait_for(asyncio.gather(holder_task, alert_task), timeout=1)
    assert calls == ["queued", "alert-admitted"]


async def test_blocked_waiter_is_admitted_without_timeout_pressure():
    """Slot waiting is unbounded by design; LLM timeouts live inside the agent."""
    manager = ConcurrencyManager(max_total=1, max_per_user=3)
    release = asyncio.Event()

    async def slow() -> None:
        async with manager.analysis_slot(user_key="user-a"):
            await release.wait()

    slow_task = asyncio.create_task(slow())
    await asyncio.sleep(0)

    async def queued() -> None:
        async with manager.analysis_slot(user_key="user-b"):
            pass

    queued_task = asyncio.create_task(queued())
    await asyncio.sleep(0.05)
    assert not queued_task.done()

    release.set()
    await asyncio.wait_for(asyncio.gather(slow_task, queued_task), timeout=1)
