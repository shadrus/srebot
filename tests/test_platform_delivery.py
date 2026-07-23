from datetime import timedelta
from itertools import count
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from aiotimebot.api.client import AuthenticatedClient
from aiotimebot.errors import APIError
from aiotimebot.retry import RetryPolicy
from aiotimebot.transport import RetryingAsyncTransport
from lxml import html as lxml_html
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse
from telegram.error import RetryAfter

from srebot.bot.delivery import (
    DeliveryReceipt,
    DeliveryStatus,
    MessageConstraints,
    html_visible_length,
    paginate_markdown,
)
from srebot.bot.discord.handlers import (
    DISCORD_MESSAGE_LIMIT,
    DiscordChatAdapter,
    _deliver_reply,
)
from srebot.bot.shared import register_followup_receipt
from srebot.bot.slack.handlers import SLACK_MESSAGE_LIMIT, SlackChatAdapter, _markdown_to_slack
from srebot.bot.slack.handlers import _deliver_text as deliver_slack
from srebot.bot.telegram.handlers import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramChatAdapter,
)
from srebot.bot.telegram.handlers import (
    _deliver_markdown as deliver_telegram,
)
from srebot.bot.telegram.html_utils import markdown_to_telegram_html
from srebot.bot.time.handlers import TimeChatAdapter, _edit_post
from srebot.bot.time.handlers import _deliver_text as deliver_time
from srebot.bot.time.integration import (
    TIME_MESSAGE_FALLBACK,
    TIME_MESSAGE_RESERVE,
    discover_time_message_limit,
)


def _long_markdown() -> str:
    return "\n\n".join(
        f"## Section {index}\n\n**Finding:** service-{index} emitted `error-{index}`. "
        + "details " * 160
        for index in range(12)
    )


def _normalized(value: str) -> str:
    return "".join(value.split())


def _oversized_alert():
    alert = MagicMock()
    alert.alertname = "Alert" * 1_100
    alert.cluster = "prod"
    alert.labels = {"job": "api"}
    return alert


@pytest.mark.parametrize(
    ("format_name", "render"),
    [
        ("discord", lambda value: value),
        ("time", lambda value: value),
        ("slack", _markdown_to_slack),
        ("telegram", markdown_to_telegram_html),
    ],
)
def test_platform_renderers_preserve_indivisible_plain_fallback(format_name, render):
    expected = r"C:\Windows\System32LongName"
    measure = html_visible_length if format_name == "telegram" else len
    chunks = paginate_markdown(
        f"**{expected}**",
        MessageConstraints(24, format_name, measure=measure),
        render,
    )

    if format_name == "telegram":
        visible = "".join(
            lxml_html.fromstring(f"<div>{chunk.rendered}</div>").text_content().removesuffix("\n")
            for chunk in chunks
        )
    elif format_name == "slack":
        visible = "".join(
            chunk.rendered.removeprefix("```\n").removesuffix("\n```") for chunk in chunks
        )
    else:
        visible = "".join(
            chunk.rendered.removeprefix("```text\n").removesuffix("\n```") for chunk in chunks
        )
    assert visible == expected


async def test_discord_delivery_respects_limit_and_returns_every_id():
    message = MagicMock()
    message.channel.id = 100
    identifiers = count(1)

    async def reply(text):
        return MagicMock(id=next(identifiers), content=text)

    message.reply = AsyncMock(side_effect=reply)
    receipt = await _deliver_reply(message, _long_markdown())

    assert receipt.delivered_chunks > 1
    assert len(receipt.message_ids) == message.reply.await_count
    assert all(len(call.args[0]) <= DISCORD_MESSAGE_LIMIT for call in message.reply.await_args_list)
    assert _normalized("".join(call.args[0] for call in message.reply.await_args_list)) == (
        _normalized(_long_markdown())
    )


async def test_discord_ambiguous_send_failure_is_not_blindly_retried():
    message = MagicMock()
    message.channel.id = 100
    message.reply = AsyncMock(side_effect=TimeoutError("unknown send outcome"))

    receipt = await _deliver_reply(message, "answer")

    assert receipt.status is DeliveryStatus.FAILED
    assert message.reply.await_count == 1


async def test_telegram_delivery_renders_valid_bounded_html():
    message = MagicMock()
    message.chat_id = -100
    message.message_id = 10
    identifiers = count(1)

    async def reply_text(_text, **_kwargs):
        return MagicMock(message_id=next(identifiers))

    message.reply_text = AsyncMock(side_effect=reply_text)
    receipt = await deliver_telegram(message, _long_markdown())

    assert receipt.delivered_chunks > 1
    delivered_text = ""
    for call in message.reply_text.await_args_list:
        rendered = call.args[0]
        text_content = lxml_html.fromstring(f"<div>{rendered}</div>").text_content()
        delivered_text += text_content
        assert len(text_content) <= TELEGRAM_MESSAGE_LIMIT
    expected_text = lxml_html.fromstring(
        f"<div>{markdown_to_telegram_html(_long_markdown())}</div>"
    ).text_content()
    assert _normalized(delivered_text) == _normalized(expected_text)


async def test_slack_delivery_respects_limit_and_threads_continuations():
    client = MagicMock()
    identifiers = count(1)

    async def post_message(**_kwargs):
        return {"ts": str(next(identifiers))}

    client.chat_postMessage = AsyncMock(side_effect=post_message)
    receipt = await deliver_slack("C1", client, _long_markdown())

    assert receipt.delivered_chunks > 1
    assert all(
        len(call.kwargs["text"]) <= SLACK_MESSAGE_LIMIT
        for call in client.chat_postMessage.await_args_list
    )
    primary_id = receipt.primary_message_id
    assert all(
        call.kwargs["thread_ts"] == primary_id
        for call in client.chat_postMessage.await_args_list[1:]
    )
    assert _normalized(
        "".join(call.kwargs["text"] for call in client.chat_postMessage.await_args_list)
    ) == _normalized(_markdown_to_slack(_long_markdown()))


async def test_slack_exhausted_placeholder_rate_limit_does_not_fallback_send():
    response = SlackResponse(
        client=MagicMock(),
        http_verb="POST",
        api_url="https://slack.test",
        req_args={},
        data={"ok": False, "error": "ratelimited"},
        headers={},
        status_code=429,
    )
    client = MagicMock()
    client.chat_update = AsyncMock(side_effect=SlackApiError("rate limited", response))
    client.chat_postMessage = AsyncMock()

    receipt = await deliver_slack("C1", client, "answer", placeholder_ts="placeholder")

    assert receipt.status is DeliveryStatus.FAILED
    client.chat_postMessage.assert_not_awaited()


async def test_telegram_exhausted_placeholder_retry_after_does_not_fallback_send():
    message = MagicMock()
    message.chat_id = -100
    message.message_id = 10
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=RetryAfter(timedelta(seconds=1)))
    message.get_bot.return_value = bot
    message.reply_text = AsyncMock()

    receipt = await deliver_telegram(message, "answer", placeholder_id=99)

    assert receipt.status is DeliveryStatus.FAILED
    message.reply_text.assert_not_awaited()


async def test_all_analyzing_placeholders_paginate_oversized_alert_fields():
    alert = _oversized_alert()

    discord_message = MagicMock()
    discord_message.channel.id = 1
    discord_ids = count(1)
    discord_message.reply = AsyncMock(
        side_effect=lambda text: MagicMock(id=next(discord_ids), content=text)
    )
    discord = DiscordChatAdapter(discord_message, dry_run=False)
    await discord.send_analyzing_placeholder("fp", "label", alert, 1)
    await discord.send_short_notification("fp", "label", alert)
    await discord.send_resolved("label", alert, "resolved", None)
    assert discord_message.reply.await_count > 3
    assert all(
        len(call.args[0]) <= DISCORD_MESSAGE_LIMIT for call in discord_message.reply.await_args_list
    )

    telegram_message = MagicMock()
    telegram_message.chat_id = -100
    telegram_message.message_id = 10
    telegram_ids = count(1)
    telegram_message.reply_text = AsyncMock(
        side_effect=lambda *_args, **_kwargs: MagicMock(message_id=next(telegram_ids))
    )
    telegram = TelegramChatAdapter(telegram_message, dry_run=False)
    await telegram.send_analyzing_placeholder("fp", "label", alert, 1)
    await telegram.send_short_notification("fp", "label", alert)
    await telegram.send_resolved("label", alert, "resolved", None)
    assert telegram_message.reply_text.await_count > 3
    assert all(
        html_visible_length(call.args[0]) <= TELEGRAM_MESSAGE_LIMIT
        for call in telegram_message.reply_text.await_args_list
    )

    slack_client = MagicMock()
    slack_ids = count(1)
    slack_client.chat_postMessage = AsyncMock(
        side_effect=lambda **_kwargs: {"ts": str(next(slack_ids))}
    )
    slack = SlackChatAdapter("C1", slack_client, dry_run=False)
    await slack.send_analyzing_placeholder("fp", "label", alert, 1)
    await slack.send_short_notification("fp", "label", alert)
    await slack.send_resolved("label", alert, "resolved", None)
    assert slack_client.chat_postMessage.await_count > 3
    assert all(
        len(call.kwargs["text"]) <= SLACK_MESSAGE_LIMIT
        for call in slack_client.chat_postMessage.await_args_list
    )

    event = MagicMock()
    event.post.channel_id = "C1"
    event.post.id = "source"
    event.post.root_id = ""
    time_client = MagicMock()
    time_client.message_limit = 120
    time_ids = count(1)
    time_client.send_message = AsyncMock(
        side_effect=lambda **_kwargs: MagicMock(id=str(next(time_ids)))
    )
    time_adapter = TimeChatAdapter(event, time_client, dry_run=False)
    await time_adapter.send_analyzing_placeholder("fp", "label", alert, 1)
    await time_adapter.send_short_notification("fp", "label", alert)
    await time_adapter.send_resolved("label", alert, "resolved", None)
    assert time_client.send_message.await_count > 3
    assert all(len(call.kwargs["text"]) <= 120 for call in time_client.send_message.await_args_list)


async def test_time_delivery_uses_runtime_limit_and_threads_continuations():
    event = MagicMock()
    event.post.channel_id = "C1"
    event.post.id = "source"
    event.post.root_id = ""
    client = MagicMock()
    client.message_limit = 120
    identifiers = count(1)

    async def send_message(**_kwargs):
        return MagicMock(id=str(next(identifiers)))

    client.send_message = AsyncMock(side_effect=send_message)
    receipt = await deliver_time(client, event, _long_markdown())

    assert receipt.delivered_chunks > 1
    assert all(len(call.kwargs["text"]) <= 120 for call in client.send_message.await_args_list)
    assert all(
        call.kwargs["root_id"] == receipt.primary_message_id
        for call in client.send_message.await_args_list[1:]
    )
    assert _normalized(
        "".join(call.kwargs["text"] for call in client.send_message.await_args_list)
    ) == _normalized(_long_markdown())


async def test_time_delivery_maps_exhausted_retry_to_partial_receipt():
    event = MagicMock()
    event.post.channel_id = "C1"
    event.post.id = "source"
    event.post.root_id = ""
    client = MagicMock()
    client.message_limit = 120
    client.send_message = AsyncMock(
        side_effect=[MagicMock(id="first"), RuntimeError("retry budget exhausted")]
    )

    receipt = await deliver_time(client, event, _long_markdown())

    assert receipt.status is DeliveryStatus.PARTIAL
    assert receipt.message_ids == ("first",)
    assert client.send_message.await_count == 2


async def test_time_edit_honors_bounded_429_retry():
    sleep = AsyncMock()
    attempts = 0

    async def handle(request):
        nonlocal attempts
        attempts += 1
        status_code = 429 if attempts < 3 else 200
        return httpx.Response(
            status_code,
            headers={"X-RateLimit-Reset": "105"},
            json={"message": "rate limited"} if status_code == 429 else {},
        )

    transport = RetryingAsyncTransport(
        httpx.MockTransport(handle),
        policy=RetryPolicy(max_attempts=3),
        sleep=sleep,
        clock=lambda: 100,
    )
    async with httpx.AsyncClient(base_url="https://time.test", transport=transport) as http_client:
        api = AuthenticatedClient(
            base_url="https://time.test",
            token="token",
            raise_on_unexpected_status=True,
        ).set_async_httpx_client(http_client)
        client = MagicMock(api=api)
        await _edit_post(client, "post-1", "updated")

    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [5.0, 5.0]


async def test_time_edit_raises_after_header_aware_retry_exhaustion():
    sleep = AsyncMock()
    attempts = 0

    async def handle(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            headers={"X-RateLimit-Reset": "2"},
            json={"message": "rate limited"},
        )

    transport = RetryingAsyncTransport(
        httpx.MockTransport(handle),
        policy=RetryPolicy(max_attempts=3),
        sleep=sleep,
        clock=lambda: 100,
    )
    async with httpx.AsyncClient(base_url="https://time.test", transport=transport) as http_client:
        api = AuthenticatedClient(
            base_url="https://time.test",
            token="token",
            raise_on_unexpected_status=True,
        ).set_async_httpx_client(http_client)
        client = MagicMock(api=api)
        with pytest.raises(APIError) as exc_info:
            await _edit_post(client, "post-1", "updated")

    assert exc_info.value.status_code == 429
    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [2.0, 2.0]


async def test_time_send_429_exhaustion_is_classified(caplog):
    event = MagicMock()
    event.post.channel_id = "C1"
    event.post.id = "source"
    event.post.root_id = ""
    client = MagicMock()
    client.message_limit = 120
    client.send_message = AsyncMock(side_effect=APIError("rate limit exhausted", status_code=429))

    with caplog.at_level("ERROR"):
        receipt = await deliver_time(client, event, "answer")

    assert receipt.status is DeliveryStatus.FAILED
    assert client.send_message.await_count == 1
    assert "reason=retry_exhausted" in caplog.text


def test_time_retry_contract_requires_idempotency_for_post():
    policy = RetryPolicy(max_attempts=3)

    assert policy.allows_retry("POST", {"idempotency_key": "bot:123"})
    assert not policy.allows_retry("POST", {})
    assert policy.allows_retry("GET", None)


async def test_followup_receipt_uses_parent_context_when_new_incident_is_missing():
    store = AsyncMock()
    store.get_followup_context.return_value = {"incident_id": "incident-parent"}
    receipt = DeliveryReceipt.from_ids(("first", "second"), 3)

    with patch("srebot.state.store.get_store", return_value=store):
        await register_followup_receipt(
            receipt,
            "group-fp",
            None,
            "slack:C1",
            additional_message_ids=("thread-root", "first"),
        )

    assert store.register_bot_message.await_args_list == [
        (("first", "group-fp"), {"incident_id": "incident-parent"}),
        (("second", "group-fp"), {"incident_id": "incident-parent"}),
        (("thread-root", "group-fp"), {"incident_id": "incident-parent"}),
    ]
    store.set_last_active_incident.assert_awaited_once_with("slack:C1", "group-fp")


async def test_followup_receipt_registers_general_query_without_incident_id():
    store = AsyncMock()
    receipt = DeliveryReceipt.from_ids(("answer",), 1)

    with patch("srebot.state.store.get_store", return_value=store):
        await register_followup_receipt(
            receipt,
            "general_query",
            None,
            "discord:C1",
        )

    store.get_followup_context.assert_not_awaited()
    store.register_bot_message.assert_awaited_once_with(
        "answer",
        "general_query",
        incident_id=None,
    )


async def test_time_limit_discovery_uses_server_value_and_fallback():
    client = MagicMock()
    client.raw_request = AsyncMock(return_value={"MaxPostSize": 16_383})
    assert await discover_time_message_limit(client) == 16_383 - TIME_MESSAGE_RESERVE

    client.raw_request.side_effect = RuntimeError("forbidden")
    assert await discover_time_message_limit(client) == TIME_MESSAGE_FALLBACK


@pytest.mark.parametrize(
    ("message_ids", "expected_chunks"),
    [
        (("one", "two"), 2),
        (("one", "two"), 3),
    ],
)
async def test_shared_workflow_registers_all_ids_from_complete_or_partial_receipt(
    message_ids,
    expected_chunks,
):
    from srebot.bot.shared import execute_alert_group_workflow
    from srebot.parser.alert_parser import Alert, AlertStatus

    store = AsyncMock()
    store.is_new.return_value = True
    store.get_status.return_value = "analyzing"
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=("analysis", "incident-1"))
    adapter = MagicMock()
    adapter.get_chat_id.return_value = "discord:C1"
    adapter.send_analyzing_placeholder = AsyncMock(
        return_value=DeliveryReceipt.from_ids(("placeholder", "placeholder-extra"), 2)
    )
    adapter.update_with_analysis = AsyncMock(
        return_value=DeliveryReceipt.from_ids(message_ids, expected_chunks)
    )
    alert = Alert(
        status=AlertStatus.FIRING,
        alertname="CPUHigh",
        cluster="prod",
        labels={"job": "api"},
        fingerprint="fp1",
    )
    settings = MagicMock(auto_analyze_alerts=True)

    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent", return_value=agent),
        patch("srebot.config.get_settings", return_value=settings),
    ):
        await execute_alert_group_workflow("fp1", [alert], adapter, dry_run=False)

    assert store.register_bot_message.await_args_list == [
        (("placeholder", "fp1"), {"incident_id": "incident-1"}),
        (("placeholder-extra", "fp1"), {"incident_id": "incident-1"}),
        (("one", "fp1"), {"incident_id": "incident-1"}),
        (("two", "fp1"), {"incident_id": "incident-1"}),
    ]
    store.mark_firing.assert_awaited_once_with("fp1", "one")


async def test_shared_short_notification_registers_every_partial_receipt_id():
    from srebot.bot.shared import execute_alert_group_workflow
    from srebot.parser.alert_parser import Alert, AlertStatus

    store = AsyncMock()
    store.is_new.return_value = True
    adapter = MagicMock()
    adapter.get_chat_id.return_value = "telegram:C1"
    adapter.send_short_notification = AsyncMock(
        return_value=DeliveryReceipt.from_ids(("short", "short-extra"), 3)
    )
    alert = Alert(
        status=AlertStatus.FIRING,
        alertname="CPUHigh",
        cluster="prod",
        labels={"job": "api"},
        fingerprint="fp1",
    )
    settings = MagicMock(auto_analyze_alerts=False)

    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent"),
        patch("srebot.config.get_settings", return_value=settings),
    ):
        await execute_alert_group_workflow("fp1", [alert], adapter, dry_run=False)

    assert store.register_bot_message.await_args_list == [
        (("short", "fp1"), {"incident_id": None}),
        (("short-extra", "fp1"), {"incident_id": None}),
    ]
    store.mark_firing.assert_awaited_once_with("fp1", "short")
