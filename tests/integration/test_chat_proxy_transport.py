"""Real local CONNECT probes; no external chat traffic or TLS tunnel is established.

The integrations construct real SDK clients. Only shared infrastructure and the
continuation after a deliberately rejected proxy handshake are replaced. These
checks prove proxy routing and authentication, not end-to-end message delivery.
"""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiotimebot import Application as TimeApplication
from aiotimebot import TimeClient
from discord.ext import commands
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from telegram.ext import Application as TelegramApplication

from srebot.bot.discord.integration import DiscordBotIntegration
from srebot.bot.slack.integration import SlackBotIntegration
from srebot.bot.telegram.integration import TelegramBotIntegration
from srebot.bot.time.integration import TimeBotIntegration
from srebot.config import Settings


@pytest.fixture
async def connect_probe(monkeypatch, request):
    for name in ("http", "https", "all", "no", "ws", "wss"):
        monkeypatch.delenv(f"{name}_proxy", raising=False)
        monkeypatch.delenv(f"{name.upper()}_PROXY", raising=False)
    received = asyncio.Queue()

    async def proxy(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            received.put_nowait(headers.decode("ascii"))
            writer.write(b"HTTP/1.1 502 Probe complete\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    authenticated = request.param
    credentials = "user:password@" if authenticated else ""
    monkeypatch.setenv("HTTPS_PROXY", f"http://{credentials}127.0.0.1:{port}")

    async def probe(operation, destination):
        task = asyncio.ensure_future(operation)
        try:
            async with asyncio.timeout(5):
                headers = await received.get()
            assert headers.splitlines()[0] == f"CONNECT {destination}:443 HTTP/1.1"
            if authenticated:
                encoded = base64.b64encode(b"user:password").decode("ascii")
                fields = {
                    name.lower(): value.strip()
                    for name, value in (
                        line.split(":", 1) for line in headers.splitlines()[1:] if line
                    )
                }
                assert fields["proxy-authorization"] == f"Basic {encoded}"
            else:
                assert "proxy-authorization:" not in headers.lower()
        finally:
            # Let the SDK consume the rejection before cancelling any retry loop.
            await asyncio.wait({task}, timeout=0.1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with server:
        yield probe


def isolate_shared_services(monkeypatch, integration, module):
    integration._register_mcp_servers = AsyncMock()
    integration._shutdown_resources = AsyncMock()
    monkeypatch.setattr(
        f"{module}.get_agent", MagicMock(return_value=MagicMock(refresh_strategies=AsyncMock()))
    )


@pytest.mark.parametrize("connect_probe", [False, True], indirect=True)
async def test_telegram_api_and_polling_use_https_proxy(monkeypatch, connect_probe):
    monkeypatch.setattr(TelegramApplication, "run_polling", lambda *args, **kwargs: None)
    integration = TelegramBotIntegration(
        Settings(telegram_bot_token="123:fake", telegram_channel_id=-100)
    )
    integration.start()
    application = integration._app
    assert application is not None
    bot = application.bot
    try:
        await connect_probe(bot.get_me(), "api.telegram.org")
        await connect_probe(bot.get_updates(), "api.telegram.org")
    finally:
        # Bot.shutdown() is a no-op before initialization succeeds; close both
        # real request pools even though this probe intentionally rejects login.
        for client_request in bot._request:
            await client_request.shutdown()


@pytest.mark.parametrize("connect_probe", [False, True], indirect=True)
async def test_slack_api_and_socket_use_https_proxy(monkeypatch, connect_probe):
    integration = SlackBotIntegration(
        Settings(slack_bot_token="xoxb-fake", slack_app_token="xapp-fake")
    )
    isolate_shared_services(monkeypatch, integration, "srebot.bot.slack.integration")

    async def probe_start(handler):
        try:
            await connect_probe(handler.app.client.api_test(), "slack.com")
            handler.client.wss_uri = "wss://gateway.slack.test/link"
            await connect_probe(handler.client.connect(), "gateway.slack.test")
        finally:
            await handler.close_async()

    monkeypatch.setattr(AsyncSocketModeHandler, "start_async", probe_start)
    await integration._run()


@pytest.mark.parametrize("connect_probe", [False, True], indirect=True)
async def test_discord_api_and_gateway_use_https_proxy(monkeypatch, connect_probe):
    integration = DiscordBotIntegration(Settings(discord_bot_token="fake"))
    isolate_shared_services(monkeypatch, integration, "srebot.bot.discord.integration")

    async def probe_start(bot, token):
        await connect_probe(bot.http.static_login(token), "discord.com")
        await connect_probe(bot.http.ws_connect("wss://gateway.discord.gg"), "gateway.discord.gg")

    monkeypatch.setattr(commands.Bot, "start", probe_start)
    await integration._run()


@pytest.mark.parametrize("connect_probe", [False, True], indirect=True)
async def test_time_api_and_events_use_https_proxy(monkeypatch, connect_probe):
    integration = TimeBotIntegration(
        Settings(time_base_url="https://time.test", time_token="fake", time_channel_id="channel")
    )
    isolate_shared_services(monkeypatch, integration, "srebot.bot.time.integration")
    raw_request = TimeClient.raw_request

    async def probe_request(client, method, path):
        if path == "/api/v4/users/me":
            await connect_probe(raw_request(client, method, path), "time.test")
            return {"id": "bot", "username": "srebot"}
        return {"MaxPostSize": 16000}

    async def probe_run(application):
        events = application.event_source.events()
        try:
            await connect_probe(anext(events), "time.test")
        finally:
            await events.aclose()

    monkeypatch.setattr(TimeClient, "raw_request", probe_request)
    monkeypatch.setattr(TimeApplication, "run", probe_run)
    await integration._run()
