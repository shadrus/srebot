import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import ClosedResourceError

from srebot.mcp.mcp_client import ExternalMCPClient


def _mock_tool_result(text: str, is_error: bool = False) -> MagicMock:
    mock_content = MagicMock()
    mock_content.text = text

    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_result.isError = is_error
    return mock_result


@pytest.mark.asyncio
async def test_call_tool_handles_exception_with_empty_str():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    # Simulate an exception with an empty string representation
    class EmptyException(Exception):
        def __str__(self):
            return ""

    client._session.call_tool.side_effect = EmptyException()

    result = await client.call_tool("some_tool", {})

    # It now returns the exception class name if message is empty
    assert json.loads(result) == {"error": "EmptyException"}


@pytest.mark.asyncio
async def test_call_tool_handles_is_error_flag_in_result():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    # Simulate a result with isError=True and some error text in content.
    client._session.call_tool.return_value = _mock_tool_result("Internal Server Error", True)

    result = await client.call_tool("some_tool", {})

    # It now returns a JSON error even if content is just text
    assert json.loads(result) == {"error": "Internal Server Error"}


@pytest.mark.asyncio
async def test_call_tool_rewrites_calendar_interval_for_elasticsearch_search():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    arguments = {
        "index": "filebeat-*",
        "query_body": {
            "aggs": {
                "by_time": {"date_histogram": {"field": "@timestamp", "calendar_interval": "15m"}},
                "by_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "1d"}},
            }
        },
    }

    # Simulate a successful tool response
    client._session.call_tool.return_value = _mock_tool_result("success")

    await client.call_tool("search", arguments)

    # Verify that client._session.call_tool was called with rewritten arguments
    called_args = client._session.call_tool.call_args[0]
    passed_name = called_args[0]
    passed_arguments = called_args[1]

    by_time_hist = passed_arguments["query_body"]["aggs"]["by_time"]["date_histogram"]
    by_day_hist = passed_arguments["query_body"]["aggs"]["by_day"]["date_histogram"]

    assert passed_name == "search"
    assert by_time_hist["fixed_interval"] == "15m"
    assert "calendar_interval" not in by_time_hist
    assert by_day_hist["calendar_interval"] == "1d"


@pytest.mark.asyncio
async def test_call_tool_reconnects_once_after_closed_resource():
    client = ExternalMCPClient("dummy_cmd")
    initial_session = AsyncMock()
    retry_session = AsyncMock()
    client._session = initial_session

    initial_session.call_tool.side_effect = ClosedResourceError()
    retry_session.call_tool.return_value = _mock_tool_result("success after reconnect")

    async def reconnect():
        client._session = retry_session

    client.connect = AsyncMock(side_effect=reconnect)

    result = await client.call_tool("some_tool", {"arg": "value"})

    assert result == "success after reconnect"
    initial_session.call_tool.assert_awaited_once_with("some_tool", {"arg": "value"})
    retry_session.call_tool.assert_awaited_once_with("some_tool", {"arg": "value"})
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_tool_returns_error_when_reconnect_retry_fails():
    client = ExternalMCPClient("dummy_cmd")
    initial_session = AsyncMock()
    retry_session = AsyncMock()
    client._session = initial_session

    initial_session.call_tool.side_effect = ClosedResourceError()
    retry_session.call_tool.side_effect = ClosedResourceError()

    async def reconnect():
        client._session = retry_session

    client.connect = AsyncMock(side_effect=reconnect)

    result = await client.call_tool("some_tool", {})

    assert json.loads(result) == {"error": "ClosedResourceError"}
    initial_session.call_tool.assert_awaited_once_with("some_tool", {})
    retry_session.call_tool.assert_awaited_once_with("some_tool", {})
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_tool_serializes_parallel_calls_on_single_client():
    client = ExternalMCPClient("dummy_cmd")

    class BlockingSession:
        def __init__(self):
            self.active_calls = 0
            self.max_active_calls = 0

        async def call_tool(self, name: str, arguments: dict):
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            await asyncio.sleep(0.01)
            self.active_calls -= 1
            return _mock_tool_result(f"{name}:{arguments['i']}")

    session = BlockingSession()
    client._session = session

    results = await asyncio.gather(
        client.call_tool("some_tool", {"i": 1}),
        client.call_tool("some_tool", {"i": 2}),
    )

    assert sorted(results) == ["some_tool:1", "some_tool:2"]
    assert session.max_active_calls == 1
