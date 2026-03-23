import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_observability_bot.mcp.mcp_client import ExternalMCPClient


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
    mock_result.isError = True # The protocol uses isError (camelCase)
    
    client._session.call_tool.return_value = mock_result
    
    result = await client.call_tool("some_tool", {})
    
    # It now returns a JSON error even if content is just text
    assert json.loads(result) == {"error": "Internal Server Error"}
