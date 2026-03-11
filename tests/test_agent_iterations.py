import pytest
from unittest.mock import AsyncMock, MagicMock
from ai_observability_bot.parser.alert_parser import Alert
from ai_observability_bot.llm.agent import AlertAnalysisAgent

@pytest.mark.asyncio
async def test_agent_includes_iteration_count(mocker):
    # Mock settings and registry to avoid actual side effects
    mocker.patch("ai_observability_bot.llm.agent.get_settings")
    mocker.patch("ai_observability_bot.llm.agent.get_mcp_registry")
    mocker.patch("ai_observability_bot.llm.agent.get_tools_schema", return_value=[])

    # Mock OpenAI client
    mock_client = mocker.AsyncMock()
    
    # Sequence of responses:
    # 1. Tool call
    # 2. Final stop
    
    # Response 1: Tool Call
    resp1 = MagicMock()
    msg1 = MagicMock()
    tool_call = MagicMock()
    tool_call.function.name = "test_tool"
    tool_call.function.arguments = "{}"
    tool_call.id = "call_1"
    msg1.tool_calls = [tool_call]
    msg1.model_dump.return_value = {"role": "assistant", "tool_calls": [{"id": "call_1"}]}
    resp1.choices = [MagicMock(message=msg1, finish_reason="tool_calls")]
    
    # Response 2: Stop
    resp2 = MagicMock()
    msg2 = MagicMock()
    msg2.tool_calls = None
    msg2.content = "My final answer."
    msg2.model_dump.return_value = {"role": "assistant", "content": "My final answer."}
    resp2.choices = [MagicMock(message=msg2, finish_reason="stop")]
    
    mock_client.chat.completions.create.side_effect = [resp1, resp2]
    
    mocker.patch("ai_observability_bot.llm.agent.AsyncOpenAI", return_value=mock_client)
    mocker.patch("ai_observability_bot.llm.agent.call_tool", AsyncMock(return_value="tool output"))
    
    agent = AlertAnalysisAgent()
    agent._max_iterations = 5
    
    alert = Alert(
        status="firing", alertname="Test", cluster="prod", labels={}, annotations={}, 
        fingerprint="1", startsAt="now", endsAt="never", generatorURL="",
        namespace="default", severity="critical"
    )
    
    result = await agent.analyze([alert])
    
    assert "My final answer." in result
    assert "🛠 Tools used (2):" in result
    assert "<code>test_tool</code>" in result
