import pytest
from unittest.mock import AsyncMock, MagicMock
from ai_observability_bot.parser.alert_parser import Alert
from ai_observability_bot.llm.agent import AlertAnalysisAgent
import ai_observability_bot.llm.agent as agent_module

@pytest.mark.asyncio
async def test_agent_soft_correction_loop_prevention(mocker, caplog):
    # Mock settings and registry
    mocker.patch("ai_observability_bot.llm.agent.get_settings")
    mocker.patch("ai_observability_bot.llm.agent.get_mcp_registry")
    mocker.patch("ai_observability_bot.llm.agent.get_tools_schema", return_value=[])

    # We want to spy on call_tool to ensure it's NOT called the second time
    mock_call_tool = mocker.patch("ai_observability_bot.llm.agent.call_tool", AsyncMock(return_value="actual tool output"))

    # Mock OpenAI client
    mock_client = mocker.AsyncMock()
    
    # Response 1: Tool Call A
    resp1 = MagicMock()
    msg1 = MagicMock()
    tool_call1 = MagicMock()
    tool_call1.function.name = "test_tool"
    tool_call1.function.arguments = '{"query": "error"}'
    tool_call1.id = "call_1"
    msg1.tool_calls = [tool_call1]
    msg1.model_dump.return_value = {"role": "assistant", "tool_calls": [{"id": "call_1"}]}
    msg1.content = None
    resp1.choices = [MagicMock(message=msg1, finish_reason="tool_calls")]
    
    # Response 2: EXACT SAME Tool Call A
    resp2 = MagicMock()
    msg2 = MagicMock()
    tool_call2 = MagicMock()
    tool_call2.function.name = "test_tool"
    tool_call2.function.arguments = '{"query": "error"}'
    tool_call2.id = "call_2"
    msg2.tool_calls = [tool_call2]
    msg2.model_dump.return_value = {"role": "assistant", "tool_calls": [{"id": "call_2"}]}
    msg2.content = None
    resp2.choices = [MagicMock(message=msg2, finish_reason="tool_calls")]

    # Response 3: Agent gives up / Final answer
    resp3 = MagicMock()
    msg3 = MagicMock()
    msg3.tool_calls = None
    msg3.content = "Okay, I give up."
    msg3.model_dump.return_value = {"role": "assistant", "content": "Okay, I give up."}
    resp3.choices = [MagicMock(message=msg3, finish_reason="stop")]

    mock_client.chat.completions.create.side_effect = [resp1, resp2, resp3]
    mocker.patch("ai_observability_bot.llm.agent.AsyncOpenAI", return_value=mock_client)
    
    agent = AlertAnalysisAgent()
    agent._max_iterations = 5
    
    alert = Alert(
        status="firing", alertname="Test", cluster="prod", labels={}, annotations={}, 
        fingerprint="1", startsAt="now", endsAt="never", generatorURL="",
        namespace="default", severity="critical"
    )
    
    result = await agent.analyze([alert])
    
    # Assertions
    assert "Okay, I give up." in result
    
    # The actual tool should only be called ONCE
    assert mock_call_tool.call_count == 1
    
    # We should have exactly one WARNING log about duplicate prevention
    warning_logs = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert any("Prevented duplicate tool call: test_tool" in msg for msg in warning_logs)

    # Inspect the messages sent to the LLM
    calls = mock_client.chat.completions.create.call_args_list
    # The 3rd call to OpenAI contains the history of what happened in response 2
    history = calls[2].kwargs["messages"]
    
    # Find the tool response for call_2
    tool_resp_for_call_2 = next(m for m in history if m.get("role") == "tool" and m.get("tool_call_id") == "call_2")
    
    assert "⚠️ Error: You already called 'test_tool' with exactly these arguments" in tool_resp_for_call_2["content"]
