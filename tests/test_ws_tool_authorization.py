import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from srebot.llm.ws_client import SaaSWSClient


class FakeWebSocket:
    """Minimal WebSocket double for one request/response analysis loop."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = [json.dumps(message) for message in messages]
        self.sent: list[dict] = []

    async def recv(self) -> str:
        return self._messages.pop(0)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class FakeConnection:
    """Async context manager returned by the mocked connect function."""

    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_args) -> None:
        return None


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "test", "parameters": {}},
    }


def _messages() -> list[dict]:
    return [
        {"event": "update_strategies", "strategies": []},
        {
            "event": "execute_tools",
            "tools": [
                {"tool_call_id": "allowed-id", "tool_name": "prod__query", "args": {}},
                {
                    "tool_call_id": "blocked-id",
                    "tool_name": "staging__query",
                    "args": {},
                },
            ],
        },
        {"event": "final_analysis", "text": "Done", "incident_id": "incident-1"},
    ]


async def test_alert_loop_executes_only_tools_from_advertised_snapshot(monkeypatch):
    websocket = FakeWebSocket(_messages())
    executor = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setattr(
        "srebot.llm.ws_client.connect",
        lambda *_args, **_kwargs: FakeConnection(websocket),
    )
    monkeypatch.setattr(
        "srebot.llm.ws_client.get_settings",
        lambda: SimpleNamespace(alert_analysis_timeout=5),
    )

    client = SaaSWSClient("wss://example.test", "token")
    content, incident_id = await client.analyze_alert(
        alert_data={"alerts": []},
        tools_schema=[_schema("prod__query")],
        tool_executor=executor,
    )

    assert incident_id == "incident-1"
    assert "`prod__query`" in content
    assert "`staging__query`" in content
    executor.assert_awaited_once_with("prod__query", "{}")
    results = websocket.sent[1]["results"]
    assert [result["tool_call_id"] for result in results] == ["allowed-id", "blocked-id"]
    assert json.loads(results[1]["data"])["error"].startswith("Tool is not authorized")


async def test_followup_loop_executes_only_tools_from_advertised_snapshot(monkeypatch):
    websocket = FakeWebSocket(_messages())
    executor = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setattr(
        "srebot.llm.ws_client.connect",
        lambda *_args, **_kwargs: FakeConnection(websocket),
    )
    monkeypatch.setattr(
        "srebot.llm.ws_client.get_settings",
        lambda: SimpleNamespace(followup_analysis_timeout=5),
    )

    client = SaaSWSClient("wss://example.test", "token")
    with patch("srebot.mcp.registry.call_tool", executor):
        content, incident_id = await client.analyze_followup(
            question="Check logs",
            rca_context="RCA",
            alert_data={"alerts": []},
            tools_schema=[_schema("prod__query")],
        )

    assert incident_id == "incident-1"
    assert "`prod__query`" in content
    assert "`staging__query`" in content
    executor.assert_awaited_once_with("prod__query", "{}")
    results = websocket.sent[1]["results"]
    assert [result["tool_call_id"] for result in results] == ["allowed-id", "blocked-id"]
    assert json.loads(results[1]["data"])["error"].startswith("Tool is not authorized")
