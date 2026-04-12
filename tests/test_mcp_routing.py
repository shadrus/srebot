from unittest.mock import AsyncMock

import pytest

from srebot.config import MCPServerConfig, MCPServerRegistry
from srebot.llm.agent import AlertAnalysisAgent
from srebot.parser.alert_parser import Alert
from srebot.parser.filtering import FilterCondition


@pytest.mark.asyncio
async def test_agent_server_routing(mocker):
    configs = {
        "prod_server": MCPServerConfig(
            name="prod_server",
            url="http://dummy",
            condition=FilterCondition(labels={"cluster": "prod"}),
        )
    }
    registry = MCPServerRegistry(configs)
    mocker.patch("srebot.llm.agent.get_mcp_registry", return_value=registry)
    mocker.patch("srebot.llm.agent.get_tools_schema", return_value=["mocked_schema"])

    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_alert = AsyncMock(return_value="Done")

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    alert_prod = Alert(
        status="firing",
        alertname="Test",
        cluster="prod",
        namespace="default",
        severity="critical",
        labels={"cluster": "prod"},
        annotations={},
        fingerprint="1",
        source_url=None,
    )

    await agent.analyze([alert_prod])

    kwargs = mock_ws.return_value.analyze_alert.call_args.kwargs
    assert kwargs["tools_schema"] == ["mocked_schema"]
