import json
from unittest.mock import AsyncMock

from srebot.llm.ws_client import (
    _execute_tool_calls,
    _is_tool_error,
    _tool_failure_notice,
    _tools_used_notice,
    _trim_tool_result,
)
from srebot.mcp.registry import _process_tool_result


def test_process_tool_result_json_list_deduplication():
    # List of identical dicts
    logs = [
        {"message": "error occurred", "user": "admin"},
        {"message": "error occurred", "user": "admin"},
        {"message": "error occurred", "user": "admin"},
        {"message": "something else", "user": "admin"},
    ]
    input_text = json.dumps(logs)
    processed = _process_tool_result(input_text)
    data = json.loads(processed)

    # Should have 2 unique items
    assert len(data) == 2
    assert data[0]["message"] == "error occurred"
    assert data[0]["_bot_occurrence_count"] == 3
    assert data[1]["message"] == "something else"
    assert "_bot_occurrence_count" not in data[1]


def test_process_tool_result_json_list_mixed_deduplication():
    # Mixed types in list
    items = ["warn", "warn", "error", {"msg": "bad"}, {"msg": "bad"}]
    input_text = json.dumps(items)
    processed = _process_tool_result(input_text)
    data = json.loads(processed)

    assert data[0] == "warn (repeated 2 times)"
    assert data[1] == "error"
    assert data[2]["msg"] == "bad"
    assert data[2]["_bot_occurrence_count"] == 2


def test_process_tool_result_truncation():
    # Very long string
    long_text = "A" * 10000
    processed = _process_tool_result(long_text, max_chars=100)
    assert len(processed) <= 180  # 100 + message
    assert "[TRUNCATED" in processed


def test_process_tool_result_json_truncation():
    # Long JSON
    data = {"logs": [{"msg": f"log {i}"} for i in range(1000)]}
    input_text = json.dumps(data)
    processed = _process_tool_result(input_text, max_chars=100)
    assert json.loads(processed)["_bot_compacted"] is True


def test_process_tool_result_compacts_large_top_level_json_list():
    data = [{"msg": f"log {i}"} for i in range(1000)]

    processed = _process_tool_result(json.dumps(data))
    result = json.loads(processed)

    assert result["_bot_compacted"] is True
    assert result["total_items"] == 1000
    assert result["returned_items"] == 50
    assert len(result["items"]) == 50


def test_process_tool_result_compacts_nested_json_lists():
    data = {
        "status": "success",
        "data": {
            "activeTargets": [{"scrapeUrl": f"http://10.0.0.{i}:9100"} for i in range(300)],
            "droppedTargets": [{"discoveredLabels": {"instance": str(i)}} for i in range(300)],
        },
    }

    processed = _process_tool_result(json.dumps(data), max_chars=12000)
    result = json.loads(processed)

    active = result["data"]["activeTargets"]
    dropped = result["data"]["droppedTargets"]
    assert active["_bot_compacted"] is True
    assert active["total_items"] == 300
    assert len(active["items"]) == 50
    assert dropped["_bot_compacted"] is True
    assert dropped["omitted_items"] == 250


def test_process_tool_result_truncates_long_json_strings():
    data = {"message": "A" * 9000}

    processed = _process_tool_result(json.dumps(data))
    result = json.loads(processed)

    assert "TRUNCATED_BY_BOT" in result["message"]


def test_process_tool_result_fallback_fits_max_chars():
    data = {
        "activeTargets": [
            {"instance": f"10.0.0.{i}:9100", "job": "node-exporter"} for i in range(1000)
        ],
        "droppedTargets": [
            {"instance": f"10.1.0.{i}:9100", "reason": "dropped"} for i in range(1000)
        ],
        "bigLog": "A" * 20000,
    }

    processed = _process_tool_result(json.dumps(data), max_chars=8000)
    result = json.loads(processed)

    assert len(processed) <= 8000
    assert result["_bot_compacted"] is True
    assert result["summary"]["items_omitted"] == 1900


def test_process_tool_result_invalid_json():
    # Invalid JSON should just be truncated raw
    invalid_json = "{ 'bad': 'json' ,,, }" * 1000
    processed = _process_tool_result(invalid_json, max_chars=50)
    assert processed.startswith("{ 'bad'")
    assert "[TRUNCATED" in processed


def test_trim_tool_result_limits_websocket_payload():
    result = _trim_tool_result("A" * 1000, max_chars=100)

    assert result.startswith("A" * 100)
    assert "[TRUNCATED_BY_BOT" in result


def test_is_tool_error_recognizes_mcp_error_envelopes():
    assert _is_tool_error('{"error": "connection refused"}') is True
    assert _is_tool_error("Error: Tool execution timed out after 60s") is True
    assert _is_tool_error('{"items": []}') is False


async def test_execute_tool_calls_reports_failure_and_keeps_successful_results():
    async def executor(name: str, _arguments: str) -> str:
        if name == "unavailable-tool":
            return '{"error": "connection refused"}'
        return '{"items": [1]}'

    callback = AsyncMock()
    results, failed_tools = await _execute_tool_calls(
        [
            {"tool_call_id": "1", "tool_name": "available-tool", "args": {}},
            {"tool_call_id": "2", "tool_name": "unavailable-tool", "args": {}},
        ],
        executor,
        " (test)",
        callback,
    )

    assert results == [
        {"tool_call_id": "1", "data": '{"items": [1]}'},
        {"tool_call_id": "2", "data": '{"error": "connection refused"}'},
    ]
    assert failed_tools == {"unavailable-tool"}
    callback.assert_awaited_once_with(["unavailable-tool"])


def test_dynamic_tool_notices_use_markdown_instead_of_platform_html():
    tools_used = _tools_used_notice("English", {"prometheus__query"})
    failure = _tool_failure_notice("Russian", {"elasticsearch__search"})

    assert tools_used == "**🛠 Tools used:** `prometheus__query`"
    assert "**Часть источников данных недоступна.**" in failure
    assert "`elasticsearch__search`" in failure
    assert "<b>" not in tools_used + failure
    assert "<code>" not in tools_used + failure
