import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from srebot.mcp.mcp_client import ExternalMCPClient


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

    # Simulate a result with isError=True and some error text in content
    mock_content = MagicMock()
    mock_content.text = "Internal Server Error"

    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_result.isError = True  # The protocol uses isError (camelCase)

    client._session.call_tool.return_value = mock_result

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
    mock_content = MagicMock()
    mock_content.text = "success"
    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_result.isError = False
    client._session.call_tool.return_value = mock_result

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
