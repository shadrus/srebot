from unittest.mock import AsyncMock

import pytest

from ai_observability_bot.llm.agent import AlertAnalysisAgent
from ai_observability_bot.parser.alert_parser import Alert


@pytest.mark.asyncio
async def test_agent_calls_ws_client(mocker):
    mocker.patch("ai_observability_bot.llm.agent.get_settings")
    mocker.patch("ai_observability_bot.llm.agent.get_mcp_registry")
    mock_ws = mocker.patch("ai_observability_bot.llm.agent.SaaSWSClient")
    mock_instance = mock_ws.return_value
    mock_instance.analyze_alert = AsyncMock(return_value="Mocked SaaS response.")
    
    agent = AlertAnalysisAgent()
    alert = Alert(status="firing", alertname="Test", cluster="prod", labels={}, annotations={}, fingerprint="1", namespace="default", severity="critical", source_url="")
    agent._token = "valid_token"
    
    result = await agent.analyze([alert])
    assert result == "Mocked SaaS response."
    mock_instance.analyze_alert.assert_called_once()
