"""MCP tool registry — manages external MCP clients and their tools."""

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

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

_MAX_TOOL_RESULT_CHARS = 8000
_MAX_JSON_LIST_ITEMS = 50
_MAX_JSON_STRING_CHARS = 1000


def _unpack_exceptions(exc: BaseException) -> list[BaseException]:
    """Recursively unwrap ExceptionGroup / TaskGroup to get leaf causes."""
    leaves: list[BaseException] = []
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            leaves.extend(_unpack_exceptions(sub))
    else:
        leaves.append(exc)
    return leaves


def _is_write_tool(tool_name: str) -> bool:
    """Return True if the tool appears to perform write/mutating operations."""
    lower = tool_name.lower()
    return any(lower.startswith(p) or p in lower for p in _WRITE_TOOL_PATTERNS)


# External MCP clients
_EXTERNAL_CLIENTS: list[Any] = []
_EXTERNAL_TOOL_SCHEMAS: list[dict] = []
_EXTERNAL_TOOL_TO_CLIENT: dict[str, Any] = {}


async def _wait_for_tcp(
    host: str,
    port: int,
    retries: int = 5,
    base_delay: float = 3.0,
) -> None:
    """
    Wait until a TCP port is reachable using plain asyncio sockets.

    This avoids anyio cancel-scope side effects that occur when the MCP
    client's streamable-http / SSE transport fails mid-handshake.

    Args:
        host: Hostname or IP to connect to.
        port: TCP port number.
        retries: Max attempts.
        base_delay: Base delay in seconds (doubles each retry).
    """
    for attempt in range(1, retries + 1):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as e:
            if attempt == retries:
                logger.error(
                    "TCP %s:%d not reachable after %d attempts: %s",
                    host,
                    port,
                    retries,
                    e,
                )
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "TCP %s:%d not reachable (attempt %d/%d): %s — retrying in %.1fs",
                host,
                port,
                attempt,
                retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)


async def register_external_mcp(
    name: str,
    url: str,
    transport: str = "sse",
    read_only: bool = False,
    connect_retries: int = 5,
    connect_retry_delay: float = 3.0,
):
    """
    Connect to an external MCP server via SSE/HTTP and register its tools with a prefix.

    Before opening the MCP session, performs a TCP readiness check with
    exponential backoff so that sidecar containers have time to start up.
    The MCP connection itself is attempted only once (after TCP is confirmed
    reachable) to avoid anyio cancel-scope corruption.

    Args:
        name: Unique server name used as tool prefix.
        url: MCP server endpoint URL.
        transport: "sse" or "http" (Streamable HTTP).
        read_only: If True, write-like tools are hidden from the LLM.
        connect_retries: Max TCP readiness attempts before giving up.
        connect_retry_delay: Base delay in seconds (doubles each retry).
    """
    from srebot.mcp.mcp_client import ExternalMCPClient

    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    logger.info("Waiting for MCP server %r at %s:%d …", name, host, port)
    await _wait_for_tcp(host, port, retries=connect_retries, base_delay=connect_retry_delay)
    logger.info("TCP port %s:%d is open — connecting MCP session", host, port)

    client = ExternalMCPClient(url, transport)
    try:
        await client.connect()
    except BaseException as e:
        causes = _unpack_exceptions(e)
        detail = "; ".join(f"{type(c).__name__}: {c}" for c in causes) if causes else str(e)
        logger.error("Failed to connect to MCP server %s after TCP ready: %s", name, detail)
        try:
            await client.close()
        except BaseException:
            logger.debug("Ignoring MCP cleanup failure after unsuccessful connect", exc_info=True)
        raise

    try:
        tools = await client.get_tools_as_openai_schema()
        prepared_tools: list[dict] = []
        prepared_mappings: dict[str, tuple[Any, str]] = {}
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

            prepared_tools.append(tool)
            prepared_mappings[prefixed_name] = (client, original_name)
            registered += 1

        mode = "read_only" if read_only else "full"
        registered_names = [tool["function"]["name"] for tool in prepared_tools]
        logger.info(
            "Registered %d tools from MCP server %r (%s mode, skipped %d write tools)",
            registered,
            name,
            mode,
            skipped,
        )
        logger.debug("  Registered tools: %s", registered_names)

        # Publish only after the complete schema has been validated and transformed.
        # No awaits occur between these mutations, so registry readers see all or nothing.
        _EXTERNAL_CLIENTS.append(client)
        _EXTERNAL_TOOL_SCHEMAS.extend(prepared_tools)
        _EXTERNAL_TOOL_TO_CLIENT.update(prepared_mappings)
    except BaseException as e:
        # ExceptionGroup (TaskGroup) wraps the real error — unwrap for clarity
        causes = _unpack_exceptions(e)
        detail = "; ".join(f"{type(c).__name__}: {c}" for c in causes) if causes else str(e)
        logger.error("Failed to register tools from MCP server %s: %s", name, detail)
        try:
            await client.close()
        except BaseException:
            logger.debug("Ignoring MCP cleanup failure after tool registration", exc_info=True)
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


def _process_tool_result(text: str, max_chars: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """
    Process raw tool output to save LLM context:
    1. Deduplicate identical items in JSON lists (common for logs).
    2. Compact oversized JSON lists/strings into a generic bot envelope.
    3. Truncate raw text as a fallback for non-JSON output.

    Args:
        text: Raw tool output.
        max_chars: Maximum serialized result length before compaction.

    Returns:
        JSON or text result safe to pass back to the LLM.
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

    compacted_data, metadata = _compact_json_result(processed_data)
    compacted_text = json.dumps(compacted_data, indent=2, ensure_ascii=False)

    if len(compacted_text) <= max_chars:
        return compacted_text

    preview_chars = max(max_chars - 1000, 0)
    fallback = {
        "_bot_compacted": True,
        "summary": {
            **metadata,
            "reason": "Compacted JSON output still exceeded max_chars.",
            "max_chars": max_chars,
            "serialized_chars": len(compacted_text),
        },
        "preview": compacted_text[:preview_chars],
        "hints": [
            "The MCP result was too large after generic compaction.",
            (
                "Use narrower tool arguments, filters, time ranges, "
                "or limits if the tool supports them."
            ),
        ],
    }
    fallback_text = json.dumps(fallback, indent=2, ensure_ascii=False)
    while len(fallback_text) > max_chars and preview_chars > 0:
        overflow = len(fallback_text) - max_chars
        preview_chars = max(preview_chars - overflow - 100, 0)
        fallback["preview"] = compacted_text[:preview_chars]
        fallback_text = json.dumps(fallback, indent=2, ensure_ascii=False)

    return fallback_text


def _compact_json_result(data: Any) -> tuple[Any, dict[str, Any]]:
    """
    Compact arbitrary JSON from third-party MCP tools without tool-specific knowledge.

    Args:
        data: Parsed JSON-compatible data.

    Returns:
        Tuple of compacted data and metadata describing what was changed.
    """
    stats = {
        "large_lists_compacted": 0,
        "strings_truncated": 0,
        "items_omitted": 0,
    }
    compacted = _compact_json_value(data, path="$", stats=stats)
    metadata = {
        "large_lists_compacted": stats["large_lists_compacted"],
        "strings_truncated": stats["strings_truncated"],
        "items_omitted": stats["items_omitted"],
    }
    return compacted, metadata


def _compact_json_value(data: Any, *, path: str, stats: dict[str, int]) -> Any:
    """Recursively compact JSON lists and long strings."""
    if isinstance(data, list):
        total = len(data)
        page = data[:_MAX_JSON_LIST_ITEMS]
        compacted_items = [
            _compact_json_value(item, path=f"{path}[{idx}]", stats=stats)
            for idx, item in enumerate(page)
        ]

        if total <= _MAX_JSON_LIST_ITEMS:
            return compacted_items

        omitted = total - len(compacted_items)
        stats["large_lists_compacted"] += 1
        stats["items_omitted"] += omitted
        return {
            "_bot_compacted": True,
            "path": path,
            "total_items": total,
            "returned_items": len(compacted_items),
            "omitted_items": omitted,
            "truncated": True,
            "items": compacted_items,
            "hints": [
                "This list was compacted by the bot before sending it to the LLM.",
                "Use narrower tool arguments, filters, pagination, or time ranges if available.",
            ],
        }

    if isinstance(data, dict):
        return {
            key: _compact_json_value(value, path=f"{path}.{key}", stats=stats)
            for key, value in data.items()
        }

    if isinstance(data, str) and len(data) > _MAX_JSON_STRING_CHARS:
        stats["strings_truncated"] += 1
        return (
            f"{data[:_MAX_JSON_STRING_CHARS]}...\n\n"
            f"[TRUNCATED_BY_BOT: string field too long ({len(data)} chars)]"
        )

    return data


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
    """Close all external MCP connections.

    Suppresses anyio cancel-scope errors during forced shutdown (SystemExit)
    since the process is already terminating.
    """
    for client in _EXTERNAL_CLIENTS:
        try:
            await client.close()
        except BaseException:
            logger.debug(
                "Ignoring MCP client close failure during shutdown",
                exc_info=True,
            )
    _EXTERNAL_CLIENTS.clear()
    _EXTERNAL_TOOL_SCHEMAS.clear()
    _EXTERNAL_TOOL_TO_CLIENT.clear()
