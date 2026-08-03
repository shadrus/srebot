from unittest.mock import AsyncMock, call

import pytest

from srebot.config import MCPServerConfig, MCPServerRegistry
from srebot.llm.agent import AlertAnalysisAgent
from srebot.parser.alert_parser import Alert
from srebot.parser.filtering import FilterCondition


def _registry() -> MCPServerRegistry:
    return MCPServerRegistry(
        {
            "prod_server": MCPServerConfig(
                name="prod_server",
                url="http://dummy",
                condition=FilterCondition(labels={"cluster": "prod"}),
            ),
            "staging_server": MCPServerConfig(
                name="staging_server",
                url="http://dummy",
                condition=FilterCondition(labels={"cluster": "staging"}),
            ),
            "shared_server": MCPServerConfig(
                name="shared_server",
                url="http://dummy",
            ),
        }
    )


def _alert(cluster: str = "prod") -> Alert:
    return Alert(
        status="firing",
        alertname="Test",
        cluster=cluster,
        namespace="default",
        severity="critical",
        labels={"cluster": cluster},
        annotations={},
        fingerprint="1",
        source_url=None,
    )


def test_registry_resolves_matching_and_unconditional_servers():
    assert _registry().allowed_server_names(_alert("prod")) == [
        "prod_server",
        "shared_server",
    ]
    assert _registry().allowed_server_names(_alert("staging")) == [
        "staging_server",
        "shared_server",
    ]


@pytest.mark.asyncio
async def test_initial_analysis_uses_shared_server_resolver(mocker):
    registry = _registry()
    mocker.patch("srebot.llm.agent.get_mcp_registry", return_value=registry)
    get_tools_schema = mocker.patch(
        "srebot.llm.agent.get_tools_schema", return_value=["mocked_schema"]
    )

    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_alert = AsyncMock(return_value="Done")

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    await agent.analyze([_alert()])

    get_tools_schema.assert_called_once_with(allowed_servers=["prod_server", "shared_server"])
    kwargs = mock_ws.return_value.analyze_alert.call_args.kwargs
    assert kwargs["tools_schema"] == ["mocked_schema"]


@pytest.mark.asyncio
async def test_incident_followup_recomputes_current_server_scope(mocker):
    registry = _registry()
    mocker.patch("srebot.llm.agent.get_mcp_registry", return_value=registry)
    get_tools_schema = mocker.patch(
        "srebot.llm.agent.get_tools_schema", return_value=["mocked_schema"]
    )
    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_followup = AsyncMock(return_value=("Done", None))

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    await agent.followup(
        question="Check logs",
        rca_text="RCA",
        alert_data=[_alert().model_dump(mode="json")],
    )

    get_tools_schema.assert_called_once_with(allowed_servers=["prod_server", "shared_server"])


@pytest.mark.asyncio
async def test_incident_followup_applies_caller_scope_as_additional_restriction(mocker):
    mocker.patch("srebot.llm.agent.get_mcp_registry", return_value=_registry())
    get_tools_schema = mocker.patch("srebot.llm.agent.get_tools_schema", return_value=[])
    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_followup = AsyncMock(return_value=("Done", None))

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    await agent.followup(
        question="Check logs",
        rca_text="RCA",
        alert_data=[_alert().model_dump(mode="json")],
        allowed_servers=["shared_server", "staging_server"],
    )

    get_tools_schema.assert_called_once_with(allowed_servers=["shared_server"])


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_data", [[], [{"alertname": "Test"}], ["invalid"]])
async def test_incident_followup_fails_closed_for_invalid_primary_alert(mocker, alert_data):
    mocker.patch("srebot.llm.agent.get_mcp_registry", return_value=_registry())
    get_tools_schema = mocker.patch("srebot.llm.agent.get_tools_schema", return_value=[])
    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_followup = AsyncMock(return_value=("Done", None))

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    await agent.followup(
        question="Check logs",
        rca_text="RCA",
        alert_data=alert_data,
        incident_scoped=True,
    )

    get_tools_schema.assert_called_once_with(allowed_servers=[])


@pytest.mark.asyncio
async def test_general_query_remains_explicitly_unscoped(mocker):
    get_tools_schema = mocker.patch("srebot.llm.agent.get_tools_schema", return_value=[])
    get_registry = mocker.patch("srebot.llm.agent.get_mcp_registry")
    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_followup = AsyncMock(return_value=("Done", None))

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    await agent.followup(
        question="What is happening?",
        rca_text="",
        alert_data=[],
        incident_scoped=False,
    )

    get_tools_schema.assert_called_once_with(allowed_servers=None)
    get_registry.assert_not_called()


@pytest.mark.asyncio
async def test_new_followup_uses_updated_registry_conditions(mocker):
    first_registry = MCPServerRegistry(
        {
            "first": MCPServerConfig(name="first", url="http://dummy"),
        }
    )
    second_registry = MCPServerRegistry(
        {
            "second": MCPServerConfig(name="second", url="http://dummy"),
        }
    )
    mocker.patch(
        "srebot.llm.agent.get_mcp_registry",
        side_effect=[first_registry, second_registry],
    )
    get_tools_schema = mocker.patch("srebot.llm.agent.get_tools_schema", return_value=[])
    mock_ws = mocker.patch("srebot.llm.agent.SaaSWSClient")
    mock_ws.return_value.analyze_followup = AsyncMock(return_value=("Done", None))

    agent = AlertAnalysisAgent()
    agent._token = "valid"
    alert_data = [_alert().model_dump(mode="json")]
    await agent.followup("first", "RCA", alert_data)
    await agent.followup("second", "RCA", alert_data)

    assert get_tools_schema.call_args_list == [
        call(allowed_servers=["first"]),
        call(allowed_servers=["second"]),
    ]
