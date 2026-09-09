import asyncio

from srebot.bot.progress import ProgressPublisher, render_progress
from srebot.messages import get_chat_message
from srebot.progress import ProgressEvent, ProgressPhase


def test_render_progress_uses_public_status_and_safe_fallback():
    assert (
        get_chat_message("analyzing_followup", "Russian", "markdown") == "⏳ *Анализирую запрос…*"
    )
    assert (
        render_progress(
            ProgressEvent(ProgressPhase.TOOL_EXECUTION, "Проверяю нагрузку"),
            "Russian",
        )
        == "⏳ *Проверяю нагрузку…*"
    )
    assert (
        render_progress(
            ProgressEvent(ProgressPhase.TOOL_EXECUTION, "https://internal.example"),
            "Russian",
        )
        == "⏳ *Получаю дополнительные данные…*"
    )
    assert (
        render_progress(
            ProgressEvent(ProgressPhase.TOOL_EXECUTION, "# Проверяю данные"),
            "Russian",
        )
        == "⏳ *Получаю дополнительные данные…*"
    )
    assert (
        render_progress(
            ProgressEvent(ProgressPhase.ANALYZING_RESULTS),
            "English",
        )
        == "⏳ *Analyzing results…*"
    )
    assert render_progress(ProgressEvent(ProgressPhase.PARTIAL_RESULTS), "English").startswith("⏳")
    assert render_progress(ProgressEvent(ProgressPhase.UNAVAILABLE_RESULTS), "English").startswith(
        "⏳"
    )


async def test_progress_publisher_coalesces_updates_inside_throttle_window():
    updates: list[str] = []
    now = 0.0
    release = asyncio.Event()

    async def update(text: str) -> None:
        updates.append(text)

    async def controlled_sleep(_delay: float) -> None:
        await release.wait()

    publisher = ProgressPublisher(
        update,
        "English",
        clock=lambda: now,
        sleep=controlled_sleep,
    )
    await publisher.publish(ProgressEvent(ProgressPhase.TOOL_EXECUTION, "Checking service health"))
    await publisher.publish(ProgressEvent(ProgressPhase.ANALYZING_RESULTS))
    await publisher.publish(ProgressEvent(ProgressPhase.TOOL_EXECUTION, "Checking recent changes"))

    assert updates == ["⏳ *Checking service health…*"]

    now = 2.0
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert updates == [
        "⏳ *Checking service health…*",
        "⏳ *Checking recent changes…*",
    ]
    await publisher.close()
