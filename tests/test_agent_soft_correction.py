from unittest.mock import AsyncMock

import pytest

from srebot.llm.agent import AlertAnalysisAgent
from srebot.parser.alert_parser import Alert


@pytest.mark.asyncio
async def test_agent_soft_correction_delegated(mocker):
    # Soft correction is now handled by the SaaS backend.
    # We just ensure the agent proxies it.
    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_alert = AsyncMock(return_value="Done")
    agent = AlertAnalysisAgent()
    agent._token = "valid"
    alert = Alert(status="firing", alertname="Test", cluster="prod", labels={}, annotations={}, fingerprint="1", namespace="default", severity="critical", source_url="")
    await agent.analyze([alert])
    mock_ws.return_value.analyze_alert.assert_called_once()
