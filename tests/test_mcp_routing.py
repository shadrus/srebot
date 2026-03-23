import pytest

import ai_observability_bot.mcp.registry as mcp_registry
from ai_observability_bot.config import MCPServerConfig, MCPServerRegistry
from ai_observability_bot.parser.alert_parser import Alert
from ai_observability_bot.parser.filtering import FilterCondition


@pytest.fixture
def mock_registry(mocker):
    # Setup some fake tools in the global registry state
    mcp_registry._EXTERNAL_TOOL_SCHEMAS = [
        {"function": {"name": "prod_server__search"}},
        {"function": {"name": "prod_server__query"}},
        {"function": {"name": "dev_server__search"}},
        {"function": {"name": "global_server__help"}},
    ]
    yield
    mcp_registry._EXTERNAL_TOOL_SCHEMAS.clear()

def test_get_tools_schema_filtering(mock_registry):
    # Test allowed servers filtering
    tools = mcp_registry.get_tools_schema(allowed_servers=["prod_server"])
    assert len(tools) == 2
    assert all(t["function"]["name"].startswith("prod_server__") for t in tools)

    tools = mcp_registry.get_tools_schema(allowed_servers=["global_server"])
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "global_server__help"

    # Test all allowed
    tools = mcp_registry.get_tools_schema(allowed_servers=None)
    assert len(tools) == 4

@pytest.mark.asyncio
async def test_agent_server_routing(mocker, mock_registry):
    # Mock settings / registry
    configs = {
        "prod_server": MCPServerConfig(
            name="prod_server", 
            command="dummy", 
            condition=FilterCondition(labels={"cluster": "prod"})
        ),
        "dev_server": MCPServerConfig(
            name="dev_server", 
            command="dummy", 
            condition=FilterCondition(labels={"cluster": "dev"})
        ),
        "global_server": MCPServerConfig(
            name="global_server", 
            command="dummy", 
            # No condition -> applies to all
        )
    }
    registry = MCPServerRegistry(configs)
    mocker.patch("ai_observability_bot.llm.agent.get_mcp_registry", return_value=registry)
    
    # Mock OpenAI client
    mock_client = mocker.AsyncMock()
    mock_client.chat.completions.create.return_value.choices[0].finish_reason = "stop"
    mock_client.chat.completions.create.return_value.choices[0].message.tool_calls = None
    mock_client.chat.completions.create.return_value.choices[0].message.content = "Done"
    mock_client.chat.completions.create.return_value.choices[0].message.model_dump.return_value = {"role": "assistant"}

    mocker.patch("ai_observability_bot.llm.agent.AsyncOpenAI", return_value=mock_client)

    from ai_observability_bot.llm.agent import AlertAnalysisAgent
    agent = AlertAnalysisAgent()

    # 1. Alert from PROD cluster
    alert_prod = Alert(
        status="firing", alertname="Test", cluster="prod", namespace="default", severity="critical",
        labels={"cluster": "prod"}, annotations={}, fingerprint="1", startsAt="2023", endsAt="2023", generatorURL=""
    )
    
    await agent.analyze([alert_prod])
    
    # Check that OpenAI was called with the right tools
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    tools_passed = call_kwargs["tools"]
    
    # Should contain prod_server and global_server tools, but NOT dev_server
    assert tools_passed is not None
    assert len(tools_passed) == 3 
    tool_names = [t["function"]["name"] for t in tools_passed]
    assert "prod_server__search" in tool_names
    assert "global_server__help" in tool_names
    assert "dev_server__search" not in tool_names

    # 2. Alert from DEV cluster
    alert_dev = Alert(
        status="firing", alertname="Test", cluster="dev", namespace="default", severity="critical",
        labels={"cluster": "dev"}, annotations={}, fingerprint="2", startsAt="2023", endsAt="2023", generatorURL=""
    )
    
    await agent.analyze([alert_dev])
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    tools_passed = call_kwargs["tools"]
    
    # Should contain dev_server and global_server tools, but NOT prod_server
    assert tools_passed is not None
    assert len(tools_passed) == 2 
    tool_names = [t["function"]["name"] for t in tools_passed]
    assert "dev_server__search" in tool_names
    assert "global_server__help" in tool_names
    assert "prod_server__search" not in tool_names
