"""MCP tool registry — manages external MCP clients and their tools."""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Tool name prefixes/substrings that indicate write operations.
# If a server is configured as read_only=True, these tools are hidden from the LLM.
_WRITE_TOOL_PATTERNS = (
    "create_",
    "delete_",
    "update_",
    "put_",
    "insert_",
    "bulk",
    "reindex",
    "clear_",
    "flush_",
    "force_merge",
    "open_",
    "close_",
    "rollover",
    "shrink",
    "split",
    "clone",
)


def _is_write_tool(tool_name: str) -> bool:
    """Return True if the tool appears to perform write/mutating operations."""
    lower = tool_name.lower()
    return any(lower.startswith(p) or p in lower for p in _WRITE_TOOL_PATTERNS)


# External MCP clients
_EXTERNAL_CLIENTS: list[Any] = []
_EXTERNAL_TOOL_SCHEMAS: list[dict] = []
_EXTERNAL_TOOL_TO_CLIENT: dict[str, Any] = {}


async def register_external_mcp(
    name: str,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    read_only: bool = False,
):
    """Connect to an external MCP server and register its tools with a prefix."""
    from srebot.mcp.mcp_client import ExternalMCPClient

    client = ExternalMCPClient(command, args, env)
    try:
        await client.connect()
        _EXTERNAL_CLIENTS.append(client)

        tools = await client.get_tools_as_openai_schema()
        registered = 0
        skipped = 0
        for tool in tools:
            original_name = tool["function"]["name"]

            # In read_only mode, hide write-capable tools from the LLM entirely
            if read_only and _is_write_tool(original_name):
                logger.debug(
                    "read_only: skipping write tool %r from server %r", original_name, name
                )
                skipped += 1
                continue

            # Prefix tool name to avoid collisions and allow cluster routing
            # e.g. query_prometheus -> yandex-production__query_prometheus
            prefixed_name = f"{name}__{original_name}"
            tool["function"]["name"] = prefixed_name

            _EXTERNAL_TOOL_SCHEMAS.append(tool)
            _EXTERNAL_TOOL_TO_CLIENT[prefixed_name] = (client, original_name)
            registered += 1

        mode = "read_only" if read_only else "full"
        registered_names = [
            t["function"]["name"]
            for t in _EXTERNAL_TOOL_SCHEMAS
            if t["function"]["name"].startswith(f"{name}__")
        ]
        logger.info(
            "Registered %d tools from MCP server %r (%s mode, skipped %d write tools)",
            registered,
            name,
            mode,
            skipped,
        )
        logger.debug("  Registered tools: %s", registered_names)
    except Exception as e:
        logger.error("Failed to connect to MCP server %s: %s", name, e)
        raise


def get_tools_schema(allowed_servers: list[str] | None = None) -> list[dict]:
    """
    Get the schema of registered external tools.
    If allowed_servers is provided, only return tools for those specific servers.
    """
    if allowed_servers is None:
        return _EXTERNAL_TOOL_SCHEMAS

    return [
        tool
        for tool in _EXTERNAL_TOOL_SCHEMAS
        if any(tool["function"]["name"].startswith(f"{server}__") for server in allowed_servers)
    ]


def _redact_secrets(text: str) -> str:
    """Mask common secrets (Bearer tokens, API keys, passwords) from tool output."""
    if not isinstance(text, str):
        return text
    # Mask Bearer tokens
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", "Bearer [REDACTED_BY_BOT]", text)
    # Mask API keys, passwords, secrets
    text = re.sub(
        r'(?i)(password|secret|api[_-]?key)["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'\1: "[REDACTED_BY_BOT]"',
        text,
    )
    return text


async def call_tool(name: str, arguments: str | dict) -> str:
    """
    Execute a tool by name on the corresponding external MCP server.
    Returns the result serialized as a JSON string.
    """
    if name in _EXTERNAL_TOOL_TO_CLIENT:
        client, original_name = _EXTERNAL_TOOL_TO_CLIENT[name]

        if isinstance(arguments, str):
            try:
                kwargs = json.loads(arguments)
            except json.JSONDecodeError as exc:
                return json.dumps({"error": f"Invalid JSON arguments: {exc}"})
        else:
            kwargs = arguments

        # Call with original name, but route via client stored for prefixed name
        result = await client.call_tool(original_name, kwargs)
        redacted = _redact_secrets(result)
        return _process_tool_result(redacted)

    return json.dumps({"error": f"Unknown tool: {name!r}"})


def _process_tool_result(text: str, max_chars: int = 8000) -> str:
    """
    Process raw tool output to save LLM context:
    1. Deduplicate identical items in JSON lists (common for logs).
    2. Truncate long strings with a summary.
    """
    if not text:
        return text

    # Try to parse as JSON for smarter deduplication
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Not JSON, just truncate if needed
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}...\n\n[TRUNCATED: output too long ({len(text)} chars)]"

    processed_data = _deduplicate_json(data)
    final_text = json.dumps(processed_data, indent=2, ensure_ascii=False)

    if len(final_text) <= max_chars:
        return final_text

    return f"{final_text[:max_chars]}...\n\n[TRUNCATED: output too long ({len(final_text)} chars)]"


def _deduplicate_json(data: Any) -> Any:
    """Recursively deduplicate items in lists while keeping counts."""
    if isinstance(data, list):
        if not data:
            return data

        # Count occurrences of unique items (serialized for hashing)
        counts = {}
        order = []
        for item in data:
            # Process sub-items first
            processed_item = _deduplicate_json(item)
            key = json.dumps(processed_item, sort_keys=True)
            if key not in counts:
                counts[key] = {"item": processed_item, "count": 1}
                order.append(key)
            else:
                counts[key]["count"] += 1

        result = []
        for key in order:
            entry = counts[key]
            item = entry["item"]
            count = entry["count"]

            if count > 1:
                # If it's a dict, add the count inside it
                if isinstance(item, dict):
                    item["_bot_occurrence_count"] = count
                else:
                    # Otherwise wrap or append (though dicts are most common for logs)
                    item = f"{item} (repeated {count} times)"
            result.append(item)
        return result

    if isinstance(data, dict):
        return {k: _deduplicate_json(v) for k, v in data.items()}

    return data


async def shutdown_mcp():
    """Close all external MCP connections."""
    for client in _EXTERNAL_CLIENTS:
        try:
            await client.close()
        except Exception as e:
            logger.warning("Error during MCP client shutdown: %s", e)
    _EXTERNAL_CLIENTS.clear()
    _EXTERNAL_TOOL_SCHEMAS.clear()
    _EXTERNAL_TOOL_TO_CLIENT.clear()
