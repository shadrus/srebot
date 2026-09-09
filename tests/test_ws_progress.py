import json
from unittest.mock import AsyncMock, MagicMock

from srebot.llm.ws_client import SaaSWSClient
from srebot.progress import ProgressEvent, ProgressPhase


async def test_alert_analysis_reports_tool_and_result_progress(mocker):
    websocket = AsyncMock()
    websocket.recv.side_effect = [
        json.dumps({"event": "update_strategies", "strategies": []}),
        json.dumps(
            {
                "event": "execute_tools",
                "tools": [{"tool_call_id": "call-1", "tool_name": "metrics__query", "args": {}}],
                "progress_text": "Проверяю нагрузку, чтобы найти источник деградации",
            }
        ),
        json.dumps({"event": "final_analysis", "text": "Готово", "incident_id": "inc-1"}),
    ]
    connection = MagicMock()
    connection.__aenter__ = AsyncMock(return_value=websocket)
    connection.__aexit__ = AsyncMock(return_value=None)
    mocker.patch("srebot.llm.ws_client.connect", return_value=connection)
    progress = AsyncMock()

    result = await SaaSWSClient("wss://example.test", "token").analyze_alert(
        alert_data={"alerts": []},
        tools_schema=[],
        tool_executor=AsyncMock(return_value='{"items": [1]}'),
        response_language="Russian",
        on_progress=progress,
    )

    assert result == (
        "Готово\n\n**🛠 Использованные инструменты:** `metrics__query`",
        "inc-1",
    )
    assert progress.await_args_list == [
        mocker.call(
            ProgressEvent(
                ProgressPhase.TOOL_EXECUTION,
                "Проверяю нагрузку, чтобы найти источник деградации",
            )
        ),
        mocker.call(ProgressEvent(ProgressPhase.ANALYZING_RESULTS)),
    ]
