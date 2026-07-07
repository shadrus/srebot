import json

from srebot.llm.ws_client import _trim_tool_result
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
