from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState
from telegram.error import RetryAfter
from telegram.ext import AIORateLimiter

from srebot.bot.slack.integration import SlackBotIntegration, _build_slack_client
from srebot.bot.telegram.integration import TelegramBotIntegration
from srebot.config import Settings


def test_telegram_integration_configures_bounded_rate_limiter():
    settings = Settings(telegram_bot_token="token", telegram_channel_id=-100)
    integration = TelegramBotIntegration(settings)
    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.rate_limiter.return_value = builder
    builder.concurrent_updates.return_value = builder
    builder.post_init.return_value = builder
    builder.post_shutdown.return_value = builder
    application = MagicMock()
    builder.build.return_value = application

    with (
        patch("srebot.bot.telegram.integration.ApplicationBuilder", return_value=builder),
        patch("srebot.bot.telegram.integration.AIORateLimiter") as limiter,
    ):
        integration.start()

    limiter.assert_called_once_with(max_retries=2)
    builder.rate_limiter.assert_called_once_with(limiter.return_value)


async def test_slack_integration_enables_bounded_429_retry_handler():
    settings = Settings(
        slack_bot_token="xoxb-token",
        slack_app_token="xapp-token",
        slack_channel_id="C1",
    )
    integration = SlackBotIntegration(settings)
    client = MagicMock()
    client.retry_handlers = []
    app = MagicMock()
    socket_handler = MagicMock()
    socket_handler.start_async = AsyncMock()
    agent = MagicMock()
    agent.refresh_strategies = AsyncMock()
    integration._register_mcp_servers = AsyncMock()

    with (
        patch(
            "srebot.bot.slack.integration.AsyncWebClient",
            return_value=client,
        ) as client_factory,
        patch("srebot.bot.slack.integration.AsyncApp", return_value=app),
        patch(
            "srebot.bot.slack.integration.AsyncSocketModeHandler",
            return_value=socket_handler,
        ),
        patch("srebot.bot.slack.integration.register_handlers"),
        patch("srebot.bot.slack.integration.get_agent", return_value=agent),
    ):
        await integration._run()

    handlers = client_factory.call_args.kwargs["retry_handlers"]
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, AsyncRateLimitErrorRetryHandler)
    assert handler.max_retry_count == 2


async def test_slack_client_does_not_retry_ambiguous_connection_failures():
    client = _build_slack_client("xoxb-token")
    request = HttpRequest(method="POST", url="https://slack.test", headers={})
    state = RetryState()

    assert len(client.retry_handlers) == 1
    assert isinstance(client.retry_handlers[0], AsyncRateLimitErrorRetryHandler)
    assert not await client.retry_handlers[0].can_retry_async(
        state=state,
        request=request,
        error=ConnectionError("ambiguous outcome"),
    )


@pytest.mark.filterwarnings(
    "ignore:Deprecated since version v22.2:telegram.warnings.PTBDeprecationWarning"
)
async def test_telegram_rate_limiter_honors_retry_after_and_stops_at_bound():
    limiter = AIORateLimiter(max_retries=2)
    callback = AsyncMock(side_effect=RetryAfter(timedelta(seconds=3)))
    sleep = AsyncMock()

    with patch("telegram.ext._aioratelimiter.asyncio.sleep", sleep):
        with pytest.raises(RetryAfter):
            await limiter.process_request(
                callback=callback,
                args=(),
                kwargs={},
                endpoint="sendMessage",
                data={"chat_id": -100},
                rate_limit_args=None,
            )

    assert callback.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [3.1, 3.1]


async def test_slack_rate_limiter_honors_retry_after_and_stops_at_bound():
    handler = AsyncRateLimitErrorRetryHandler(max_retry_count=2)
    request = HttpRequest(method="POST", url="https://slack.test", headers={})
    response = HttpResponse(status_code=429, headers={"Retry-After": "3"})
    state = RetryState()
    sleep = AsyncMock()

    assert await handler.can_retry_async(state=state, request=request, response=response)
    with (
        patch(
            "slack_sdk.http_retry.builtin_async_handlers.asyncio.sleep",
            sleep,
        ),
        patch(
            "slack_sdk.http_retry.builtin_async_handlers.random.random",
            return_value=0.25,
        ),
    ):
        await handler.prepare_for_next_attempt_async(
            state=state,
            request=request,
            response=response,
        )

    sleep.assert_awaited_once_with(3.25)
    assert state.current_attempt == 1
    state.current_attempt = 2
    assert not await handler.can_retry_async(state=state, request=request, response=response)
