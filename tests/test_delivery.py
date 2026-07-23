import asyncio
from unittest.mock import AsyncMock

import markdown
import pytest
from lxml import html as lxml_html

from srebot.bot.delivery import (
    DeliveryCoordinator,
    DeliveryReceipt,
    DeliveryStatus,
    MessageConstraints,
    html_visible_length,
    paginate_markdown,
)


def _visible_content(value: str) -> str:
    for marker in ("```python", "```", "**", "*", "_", "`"):
        value = value.replace(marker, "")
    return "".join(value.split())


def test_message_constraints_reject_invalid_limit():
    with pytest.raises(ValueError, match="positive"):
        MessageConstraints(0, "markdown")


def test_delivery_receipt_validates_status():
    with pytest.raises(ValueError, match="status"):
        DeliveryReceipt(DeliveryStatus.COMPLETE, (), 1)


def test_empty_delivery_is_failed():
    receipt = DeliveryReceipt.from_ids((), 0)

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.primary_message_id is None


def test_paginate_fitting_content_returns_one_chunk():
    chunks = paginate_markdown("short **answer**", MessageConstraints(100, "markdown"))
    assert [chunk.rendered for chunk in chunks] == ["short **answer**"]


def test_paginate_prefers_markdown_blocks_and_preserves_content():
    text = "First paragraph with **bold** text.\n\nSecond paragraph with [link](https://x.test)."
    chunks = paginate_markdown(text, MessageConstraints(48, "markdown"))
    assert len(chunks) == 2
    assert all(len(chunk.rendered) <= 48 for chunk in chunks)
    assert _visible_content("".join(chunk.markdown for chunk in chunks)) == _visible_content(text)


def test_paginate_preserves_plain_text_boundary_whitespace_exactly():
    text = "word1     word2"
    chunks = paginate_markdown(text, MessageConstraints(9, "markdown"))

    assert len(chunks) > 1
    assert all(len(chunk.rendered) <= 9 for chunk in chunks)
    assert "".join(chunk.markdown for chunk in chunks) == text


def test_indivisible_inline_fallback_preserves_visible_backslashes():
    text = r"**C:\Windows\System32LongName**"
    chunks = paginate_markdown(text, MessageConstraints(12, "markdown"))
    rendered_html = markdown.markdown("".join(chunk.markdown for chunk in chunks))

    assert lxml_html.fromstring(rendered_html).text_content() == r"C:\Windows\System32LongName"


def test_indivisible_strikethrough_falls_back_to_plain_text():
    visible_text = "strikethrough content that crosses several chunks"
    chunks = paginate_markdown(
        f"~~{visible_text}~~",
        MessageConstraints(14, "markdown"),
    )

    assert len(chunks) > 1
    assert all(chunk.markdown.startswith("```text\n") for chunk in chunks)
    assert all(chunk.markdown.endswith("\n```") for chunk in chunks)
    assert (
        "".join(chunk.markdown.removeprefix("```text\n").removesuffix("\n```") for chunk in chunks)
        == visible_text
    )
    assert all("~~" not in chunk.markdown for chunk in chunks)


def test_paginate_reopens_oversized_fenced_code():
    body = "\n".join(f"print({index})" for index in range(20))
    text = f"```python\n{body}\n```"
    chunks = paginate_markdown(text, MessageConstraints(60, "markdown"))
    assert len(chunks) > 1
    assert all(chunk.rendered.startswith("```python\n") for chunk in chunks)
    assert all(chunk.rendered.endswith("\n```") for chunk in chunks)
    assert "".join(
        chunk.markdown.removeprefix("```python\n").removesuffix("\n```") for chunk in chunks
    ).replace("\n", "") == body.replace("\n", "")


def test_paginate_preserves_fenced_code_whitespace_exactly():
    body = (
        "def inspect():\n"
        "    if ready:\n"
        "        return {\n"
        '            "status": "ok",  \n'
        "        }\n"
        "\n"
        "service:\n"
        "  nested:\n"
        "    enabled: true  "
    )
    chunks = paginate_markdown(
        f"```python\n{body}\n```",
        MessageConstraints(54, "markdown"),
    )

    assert len(chunks) > 1
    assert all(chunk.markdown.startswith("```python\n") for chunk in chunks)
    assert all(chunk.markdown.endswith("\n```") for chunk in chunks)
    reconstructed = "".join(
        chunk.markdown.removeprefix("```python\n").removesuffix("\n```") for chunk in chunks
    )
    assert reconstructed == body


@pytest.mark.parametrize(
    ("opening_marker", "non_closing_line", "closing_marker"),
    [
        ("```", "```example", "````"),
        ("````", "```", "````"),
        ("```", "\t```", "```"),
        ("```", "    ```", "```"),
    ],
)
def test_paginate_recognizes_only_valid_closing_fence_lines(
    opening_marker: str,
    non_closing_line: str,
    closing_marker: str,
):
    body = f"before\n{non_closing_line}\nafter\n" + "\n".join(
        f"value_{index}" for index in range(8)
    )
    opening = f"{opening_marker}python\n"
    chunks = paginate_markdown(
        f"{opening}{body}\n{closing_marker}",
        MessageConstraints(42, "markdown"),
    )

    assert len(chunks) > 1
    assert all(chunk.markdown.startswith(opening) for chunk in chunks)
    assert all(chunk.markdown.endswith(f"\n{opening_marker}") for chunk in chunks)
    reconstructed = "".join(
        chunk.markdown.removeprefix(opening).removesuffix(f"\n{opening_marker}") for chunk in chunks
    )
    assert reconstructed == body


@pytest.mark.parametrize("marker", ["```", "~~~"])
def test_paginate_preserves_valid_indented_fence(marker: str):
    body = "\n".join(f"value_{index}" for index in range(10))
    opening = f"  {marker}python\n"
    closing = f"\n  {marker}"
    chunks = paginate_markdown(
        f"{opening}{body}{closing}",
        MessageConstraints(42, "markdown"),
    )

    assert len(chunks) > 1
    assert all(chunk.markdown.startswith(opening) for chunk in chunks)
    assert all(chunk.markdown.endswith(closing) for chunk in chunks)
    assert (
        "".join(chunk.markdown.removeprefix(opening).removesuffix(closing) for chunk in chunks)
        == body
    )


def test_paginate_keeps_emoji_graphemes_together():
    family = "👨‍👩‍👧‍👦"
    chunks = paginate_markdown(family * 4, MessageConstraints(len(family) * 2, "markdown"))
    assert "".join(chunk.markdown for chunk in chunks) == family * 4
    assert all(not chunk.markdown.startswith("\u200d") for chunk in chunks)


def test_paginate_keeps_regional_indicator_flags_together():
    flag = "🇷🇺"
    chunks = paginate_markdown(flag * 3, MessageConstraints(3, "markdown"))

    assert "".join(chunk.markdown for chunk in chunks) == flag * 3
    assert [chunk.markdown for chunk in chunks] == [flag, flag, flag]


@pytest.mark.parametrize(
    "grapheme",
    [
        "1️⃣",
        "कि",
        "👨‍👩‍👧‍👦",
        "🇷🇺",
        "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    ],
)
def test_paginate_uses_extended_unicode_grapheme_boundaries(grapheme: str):
    chunks = paginate_markdown(
        grapheme * 3,
        MessageConstraints(len(grapheme) + 1, "markdown"),
    )

    assert [chunk.markdown for chunk in chunks] == [grapheme, grapheme, grapheme]


def test_paginate_safely_renders_oversized_table_as_fenced_text():
    table = "| Name | Value |\n| --- | --- |\n" + "\n".join(
        f"| service-{index} | value-{index} |" for index in range(8)
    )
    chunks = paginate_markdown(table, MessageConstraints(58, "markdown"))

    assert len(chunks) > 1
    assert all(chunk.markdown.startswith("```text\n") for chunk in chunks)
    assert all(chunk.markdown.endswith("\n```") for chunk in chunks)
    assert (
        "".join(chunk.markdown.removeprefix("```text\n").removesuffix("\n```") for chunk in chunks)
        == table
    )


def test_paginate_preserves_oversized_list_content():
    text = "\n".join(f"- item {index} with details" for index in range(8))
    chunks = paginate_markdown(text, MessageConstraints(38, "markdown"))

    assert len(chunks) > 1
    assert "".join(chunk.markdown for chunk in chunks) == text


def test_paginate_uses_rendered_measurement():
    constraints = MessageConstraints(8, "telegram", measure=html_visible_length)
    chunks = paginate_markdown(
        "**abcd** **efgh**",
        constraints,
        render=lambda value: value.replace("**", "<b>", 1).replace("**", "</b>", 1),
    )
    assert all(html_visible_length(chunk.rendered) <= 8 for chunk in chunks)


async def test_coordinator_edits_first_and_sends_continuations():
    coordinator = DeliveryCoordinator()
    chunks = paginate_markdown("a b c d", MessageConstraints(3, "markdown"))
    sent: list[tuple[str, str | None]] = []

    async def edit_first(text: str) -> str:
        sent.append((text, "edit"))
        return "placeholder"

    async def send(text: str, root: str | None) -> str:
        sent.append((text, root))
        return f"message-{len(sent)}"

    receipt = await coordinator.deliver("discord:1", chunks, send, edit_first)

    assert receipt.status is DeliveryStatus.COMPLETE
    assert receipt.primary_message_id == "placeholder"
    assert all(root == "placeholder" for _, root in sent[1:])


async def test_coordinator_reports_empty_delivery_as_failed():
    coordinator = DeliveryCoordinator()
    send = AsyncMock()

    receipt = await coordinator.deliver("slack:C1", (), send)

    assert receipt.status is DeliveryStatus.FAILED
    send.assert_not_awaited()


async def test_coordinator_falls_back_after_edit_failure():
    coordinator = DeliveryCoordinator()
    chunks = paginate_markdown("answer", MessageConstraints(20, "markdown"))

    async def edit_first(_text: str) -> str:
        raise RuntimeError("cannot edit")

    async def send(_text: str, root: str | None) -> str:
        assert root is None
        return "new-message"

    receipt = await coordinator.deliver("telegram:1", chunks, send, edit_first)
    assert receipt.message_ids == ("new-message",)


async def test_coordinator_reports_partial_delivery_without_replay(caplog):
    coordinator = DeliveryCoordinator()
    chunks = paginate_markdown("one two three", MessageConstraints(5, "markdown"))
    calls = 0

    async def send(_text: str, _root: str | None) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("rate limit exhausted")
        return "first"

    with caplog.at_level("ERROR"):
        receipt = await coordinator.deliver("slack:C1", chunks, send)
    assert receipt.status is DeliveryStatus.PARTIAL
    assert receipt.message_ids == ("first",)
    assert calls == 2
    assert "reason=retry_exhausted" in caplog.text


async def test_coordinator_serializes_same_destination_and_allows_other_destination():
    coordinator = DeliveryCoordinator()
    one_chunk = paginate_markdown("one", MessageConstraints(10, "markdown"))
    events: list[str] = []
    release = asyncio.Event()

    async def slow_send(_text: str, _root: str | None) -> str:
        events.append("slow-start")
        await release.wait()
        events.append("slow-end")
        return "slow"

    async def queued_send(_text: str, _root: str | None) -> str:
        events.append("queued")
        return "queued"

    async def other_send(_text: str, _root: str | None) -> str:
        events.append("other")
        return "other"

    slow = asyncio.create_task(coordinator.deliver("time:C1", one_chunk, slow_send))
    await asyncio.sleep(0)
    queued = asyncio.create_task(coordinator.deliver("time:C1", one_chunk, queued_send))
    other = asyncio.create_task(coordinator.deliver("time:C2", one_chunk, other_send))
    await asyncio.sleep(0)
    assert events == ["slow-start", "other"]
    release.set()
    await asyncio.gather(slow, queued, other)
    assert events == ["slow-start", "other", "slow-end", "queued"]
    assert coordinator._locks == {}
    assert coordinator._lock_users == {}


async def test_coordinator_discards_destination_lock_after_failed_delivery():
    coordinator = DeliveryCoordinator()
    chunks = paginate_markdown("answer", MessageConstraints(10, "markdown"))
    send = AsyncMock(side_effect=RuntimeError("send failed"))

    await coordinator.deliver("telegram:temporary", chunks, send)

    assert coordinator._locks == {}
    assert coordinator._lock_users == {}
