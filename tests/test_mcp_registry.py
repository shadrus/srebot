from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.mcp import registry


@pytest.fixture(autouse=True)
def clear_external_registry():
    registry._EXTERNAL_CLIENTS.clear()
    registry._EXTERNAL_TOOL_SCHEMAS.clear()
    registry._EXTERNAL_TOOL_TO_CLIENT.clear()
    yield
    registry._EXTERNAL_CLIENTS.clear()
    registry._EXTERNAL_TOOL_SCHEMAS.clear()
    registry._EXTERNAL_TOOL_TO_CLIENT.clear()


async def test_registration_failure_closes_client_without_partial_registry_state():
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.get_tools_as_openai_schema = AsyncMock(
        return_value=[
            {
                "type": "function",
                "function": {
                    "name": "valid_tool",
                    "description": "Valid tool",
                    "parameters": {},
                },
            },
            {"type": "function", "function": {}},
        ]
    )

    with (
        patch("srebot.mcp.registry._wait_for_tcp", AsyncMock()),
        patch("srebot.mcp.mcp_client.ExternalMCPClient", return_value=client),
        pytest.raises(KeyError),
    ):
        await registry.register_external_mcp("broken", "http://mcp.example/sse")

    client.close.assert_awaited_once()
    assert registry._EXTERNAL_CLIENTS == []
    assert registry._EXTERNAL_TOOL_SCHEMAS == []
    assert registry._EXTERNAL_TOOL_TO_CLIENT == {}
