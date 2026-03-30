import json

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
    assert len(processed) <= 180 # 100 + message
    assert "[TRUNCATED" in processed

def test_process_tool_result_json_truncation():
    # Long JSON
    data = {"logs": [{"msg": f"log {i}"} for i in range(1000)]}
    input_text = json.dumps(data)
    processed = _process_tool_result(input_text, max_chars=100)
    assert "[TRUNCATED" in processed

def test_process_tool_result_invalid_json():
    # Invalid JSON should just be truncated raw
    invalid_json = "{ 'bad': 'json' ,,, }" * 1000
    processed = _process_tool_result(invalid_json, max_chars=50)
    assert processed.startswith("{ 'bad'")
    assert "[TRUNCATED" in processed
