import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect

from srebot.config import get_settings
from srebot.messages import get_chat_message
from srebot.parser.alert_parser import update_remote_strategies

logger = logging.getLogger(__name__)

_MAX_TOOL_RESULT_CHARS = 8000

ToolFailureCallback = Callable[[list[str]], Awaitable[None]]


def _trim_tool_result(result: str, max_chars: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """
    Limit tool output before sending it back over the SaaS WebSocket.

    Args:
        result: Serialized tool result.
        max_chars: Maximum number of characters to keep.

    Returns:
        Original or truncated result string.
    """
    if len(result) <= max_chars:
        return result

    return (
        f"{result[:max_chars]}...\n\n[TRUNCATED_BY_BOT: tool output too long ({len(result)} chars)]"
    )


def _is_tool_error(result: str) -> bool:
    """Return whether a serialized tool result represents a failure."""
    try:
        payload = json.loads(result)
    except json.JSONDecodeError, TypeError:
        return result.lstrip().lower().startswith("error:")
    return isinstance(payload, dict) and bool(payload.get("error"))


def _tool_failure_notice(response_language: str, failed_tools: set[str]) -> str:
    """Build a Markdown warning about unavailable data sources."""
    return get_chat_message("mcp_failure_result", response_language, "markdown").format(
        tools=_format_tool_names(failed_tools)
    )


def _tools_used_notice(response_language: str, used_tools: set[str]) -> str:
    """Build a Markdown summary of tools used during analysis."""
    return get_chat_message("tools_used", response_language, "markdown").format(
        tools=_format_tool_names(used_tools)
    )


def _format_tool_names(tool_names: set[str]) -> str:
    """Format tool names as inline Markdown code."""
    return ", ".join(f"`{name}`" for name in sorted(tool_names))


def _tool_names_from_schema(tools_schema: list[dict[str, Any]]) -> frozenset[str]:
    """Extract the exact function names authorized by one advertised tool schema.

    Args:
        tools_schema: OpenAI-compatible tools advertised for one analysis request.

    Returns:
        Immutable exact-name authorization snapshot for the request.
    """
    names = set()
    for tool in tools_schema:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return frozenset(names)


async def _execute_tool_calls(
    tools: list[dict[str, Any]],
    tool_executor: Any,
    log_context: str,
    allowed_tool_names: frozenset[str],
    on_tool_failure: ToolFailureCallback | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Execute one SaaS tool batch and report failures without aborting the analysis."""

    async def run_tool(tool_call: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        tool_call_id = str(tool_call.get("tool_call_id", ""))
        tool_name = str(tool_call.get("tool_name", ""))
        tool_args = tool_call.get("args", {})
        logger.info("SaaS requested tool execution%s: %s", log_context, tool_name)

        if tool_name not in allowed_tool_names:
            logger.warning(
                "Rejected tool outside request authorization snapshot%s: %s",
                log_context,
                tool_name,
            )
            result_str = json.dumps({"error": "Tool is not authorized for this analysis request"})
            return {"tool_call_id": tool_call_id, "data": result_str}, tool_name

        try:
            args_str = json.dumps(tool_args) if isinstance(tool_args, dict) else tool_args
            result = await asyncio.wait_for(tool_executor(tool_name, args_str), timeout=60)
            result_str = _trim_tool_result(str(result))
        except TimeoutError:
            logger.error("Tool %s timed out after 60s", tool_name)
            result_str = "Error: Tool execution timed out after 60s"
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            result_str = f"Error: {exc}"

        failed_name = tool_name if _is_tool_error(result_str) else None
        return {"tool_call_id": tool_call_id, "data": result_str}, failed_name

    executed = await asyncio.gather(*(run_tool(tool_call) for tool_call in tools))
    results = [result for result, _failed_name in executed]
    failed_tools = {failed_name for _result, failed_name in executed if failed_name}

    if failed_tools and on_tool_failure:
        try:
            await on_tool_failure(sorted(failed_tools))
        except Exception:
            logger.warning("Could not publish MCP failure progress update", exc_info=True)

    return results, failed_tools


async def _send_ws_json(websocket: Any, payload: dict[str, Any], context: str) -> None:
    """
    Send a JSON WebSocket text frame and log its serialized size.

    Args:
        websocket: Connected WebSocket client.
        payload: Event payload to serialize.
        context: Short caller context for logs.
    """
    event = payload.get("event", "unknown")
    raw = json.dumps(payload)
    logger.info(
        "WS send (%s): event=%s chars=%d bytes=%d",
        context,
        event,
        len(raw),
        len(raw.encode()),
    )
    await websocket.send(raw)


async def _recv_ws_json(websocket: Any, context: str) -> dict[str, Any]:
    """
    Receive a JSON WebSocket text frame and log its raw size.

    Args:
        websocket: Connected WebSocket client.
        context: Short caller context for logs.

    Returns:
        Parsed JSON object.
    """
    raw = await websocket.recv()
    if isinstance(raw, bytes):
        raw_text = raw.decode()
        byte_count = len(raw)
    else:
        raw_text = raw
        byte_count = len(raw_text.encode())

    msg = json.loads(raw_text)
    event = msg.get("event", "unknown") if isinstance(msg, dict) else "non_object"
    logger.info(
        "WS receive (%s): event=%s chars=%d bytes=%d",
        context,
        event,
        len(raw_text),
        byte_count,
    )
    return msg


class SaaSWSClient:
    ws_url: str
    token: str

    def __init__(self, ws_url: str, token: str) -> None:
        self.ws_url = ws_url
        self.token = token

    async def _handle_server_event(self, event_data: dict[str, Any]) -> bool:
        """Handle non-request-specific events like strategy updates. Returns True if handled."""
        event = event_data.get("event")
        if event == "update_strategies":
            strategies = event_data.get("strategies", [])
            update_remote_strategies(strategies)
            return True
        return False

    async def analyze_alert(
        self,
        alert_data: dict[str, Any],
        tools_schema: list[dict[str, Any]],
        tool_executor: Any,
        response_language: str = "English",
    ) -> tuple[str, str | None]:
        settings = get_settings()
        timeout = settings.alert_analysis_timeout
        try:
            logger.info("Connecting to SaaS Control Plane at %s...", self.ws_url)
            async with asyncio.timeout(timeout):
                async with connect(
                    self.ws_url,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as websocket:
                    # 1. Wait for initial strategies (always sent by server on connect)
                    # and then send the initial alert data
                    while True:
                        msg = await _recv_ws_json(websocket, "alert.init")
                        if await self._handle_server_event(msg):
                            break  # Got strategies, can proceed
                        else:
                            # If server sent something else first, it's unexpected here
                            logger.warning(
                                "Unexpected event before strategies: %s",
                                msg.get("event"),
                            )
                            continue

                    payload = {
                        "event": "analyze_alert",
                        "alert_data": alert_data,
                        "tools": tools_schema,
                        "response_language": response_language,
                    }
                    await _send_ws_json(websocket, payload, "alert.start")

                    # 2. Loop to handle Server Events
                    allowed_tool_names = _tool_names_from_schema(tools_schema)
                    used_tools: set[str] = set()
                    failed_tools: set[str] = set()
                    while True:
                        response = await _recv_ws_json(websocket, "alert.loop")
                        event = response.get("event")

                        if event == "final_analysis":
                            content = response.get("text", "")
                            incident_id = response.get("incident_id")
                            if used_tools:
                                content += "\n\n" + _tools_used_notice(
                                    response_language, used_tools
                                )
                            if failed_tools:
                                content += "\n\n" + _tool_failure_notice(
                                    response_language, failed_tools
                                )
                            return content, incident_id

                        elif event == "execute_tools":
                            tools = response.get("tools", [])

                            used_tools.update(
                                str(tool.get("tool_name", ""))
                                for tool in tools
                                if tool.get("tool_name") in allowed_tool_names
                            )
                            results, batch_failures = await _execute_tool_calls(
                                tools,
                                tool_executor,
                                "",
                                allowed_tool_names,
                            )
                            failed_tools.update(batch_failures)
                            result_payload = {"event": "tools_result", "results": results}
                            await _send_ws_json(websocket, result_payload, "alert.tools")

                        elif event == "error":
                            msg = response.get("message")
                            logger.error("SaaS Error: %s", msg)
                            return f"⚠️ Control Plane Error: {msg}", None

                        elif await self._handle_server_event(response):
                            continue

                        else:
                            logger.warning("Unknown event from SaaS: %s", event)
        except TimeoutError:
            logger.error("Analysis timed out after %d seconds", timeout)
            return (
                "⚠️ <b>Analysis timed out:</b> The AI took too long to respond. "
                "Please investigate manually."
            ), None
        except json.JSONDecodeError as exc:
            logger.exception("Invalid JSON received from SaaS during alert analysis")
            return f"⚠️ Invalid response from AI Control Plane: {exc}", None
        except Exception as exc:
            logger.exception("WebSocket connection to SaaS failed during alert analysis")
            return f"⚠️ Failed to connect to AI Control Plane: {exc}", None

    async def analyze_followup(
        self,
        question: str,
        rca_context: str,
        alert_data: dict,
        tools_schema: list[dict],
        parent_incident_id: str | None = None,
        response_language: str = "English",
        user_name: str | None = None,
        on_tool_failure: ToolFailureCallback | None = None,
    ) -> tuple[str, str | None]:
        """
        Send a follow-up question to the SaaS Control Plane with RCA context.

        Args:
            question: The engineer's follow-up question.
            rca_context: The previous RCA text produced by the bot.
            alert_data: Original alert data dict (for tool routing).
            tools_schema: List of OpenAI tool schemas allowed for this cluster.
            response_language: Language for the LLM response.
            user_name: Username or display name of the user asking the question.
            on_tool_failure: Optional callback invoked with failed MCP tool names.

        Returns:
            The bot's follow-up answer as a string.
        """
        settings = get_settings()
        timeout = settings.followup_analysis_timeout
        failed_tools: set[str] = set()
        try:
            logger.info("Connecting to SaaS Control Plane for follow-up analysis...")
            async with asyncio.timeout(timeout):
                async with connect(
                    self.ws_url,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as websocket:
                    # Wait for initial strategies
                    while True:
                        msg = await _recv_ws_json(websocket, "followup.init")
                        if await self._handle_server_event(msg):
                            break
                        else:
                            logger.warning(
                                "Unexpected event before strategies (follow-up): %s",
                                msg.get("event"),
                            )
                            continue

                    payload = {
                        "event": "followup_question",
                        "question": question,
                        "rca_context": rca_context,
                        "alert_data": alert_data,
                        "tools": tools_schema,
                        "parent_incident_id": parent_incident_id,
                        "response_language": response_language,
                        "user_name": user_name,
                    }
                    await _send_ws_json(websocket, payload, "followup.start")

                    allowed_tool_names = _tool_names_from_schema(tools_schema)
                    used_tools: set[str] = set()
                    while True:
                        response = await _recv_ws_json(websocket, "followup.loop")
                        event = response.get("event")

                        if event == "final_analysis":
                            content = response.get("text", "")
                            incident_id = response.get("incident_id")
                            if used_tools:
                                content += "\n\n" + _tools_used_notice(
                                    response_language, used_tools
                                )
                            if failed_tools:
                                content += "\n\n" + _tool_failure_notice(
                                    response_language, failed_tools
                                )
                            return content, incident_id

                        elif event == "execute_tools":
                            tools = response.get("tools", [])

                            from srebot.mcp.registry import call_tool

                            used_tools.update(
                                str(tool.get("tool_name", ""))
                                for tool in tools
                                if tool.get("tool_name") in allowed_tool_names
                            )
                            results, batch_failures = await _execute_tool_calls(
                                tools,
                                call_tool,
                                " (follow-up)",
                                allowed_tool_names,
                                on_tool_failure,
                            )
                            failed_tools.update(batch_failures)
                            result_payload = {"event": "tools_result", "results": results}
                            await _send_ws_json(websocket, result_payload, "followup.tools")

                        elif event == "error":
                            msg = response.get("message")
                            logger.error("SaaS Error (follow-up): %s", msg)
                            return f"⚠️ Control Plane Error: {msg}", None

                        elif await self._handle_server_event(response):
                            continue

                        else:
                            logger.warning("Unknown event from SaaS (follow-up): %s", event)

        except TimeoutError:
            logger.error("Follow-up analysis timed out after %d seconds", timeout)
            message = (
                "⚠️ <b>Analysis timed out:</b> The AI took too long to respond. Please try again."
            )
            if failed_tools:
                message += "\n\n" + _tool_failure_notice(response_language, failed_tools)
            return message, None
        except json.JSONDecodeError as exc:
            logger.exception("Invalid JSON received from SaaS during follow-up analysis")
            return f"⚠️ Invalid response from AI Control Plane: {exc}", None
        except Exception as exc:
            logger.exception("WebSocket connection to SaaS failed during follow-up analysis")
            return f"⚠️ Failed to connect to AI Control Plane: {exc}", None

    async def extract_alerts(self, text: str) -> list[dict[str, Any]]:
        """Request the SaaS Control Plane to parse raw text into structured Alert objects."""
        try:
            logger.info("Connecting to SaaS Control Plane for smart parsing...")
            async with connect(
                self.ws_url, additional_headers={"Authorization": f"Bearer {self.token}"}
            ) as websocket:
                # Wait for strategies
                while True:
                    msg = await _recv_ws_json(websocket, "extract.init")
                    if await self._handle_server_event(msg):
                        break

                payload = {
                    "event": "extract_alerts",
                    "text": text,
                }
                await _send_ws_json(websocket, payload, "extract.start")

                while True:
                    response = await _recv_ws_json(websocket, "extract.loop")
                    event = response.get("event")

                    if event == "extracted_alerts":
                        return response.get("alerts", [])
                    elif event == "error":
                        msg = response.get("message")
                        logger.error("SaaS Extraction Error: %s", msg)
                        return []
                    elif await self._handle_server_event(response):
                        continue
                    else:
                        logger.warning("Unknown event from SaaS during extraction: %s", event)
                        return []
        except json.JSONDecodeError:
            logger.exception("Invalid JSON received from SaaS during smart parsing")
            return []
        except Exception:
            logger.exception("Smart parsing via SaaS failed")
            return []

    async def refresh_strategies(self) -> None:
        """Connect briefly to the SaaS Control Plane to fetch the latest parsing strategies."""
        try:
            logger.info("Connecting to SaaS Control Plane to refresh parsing strategies...")
            async with connect(
                self.ws_url, additional_headers={"Authorization": f"Bearer {self.token}"}
            ) as websocket:
                # The server sends 'update_strategies' immediately after accept
                msg = await _recv_ws_json(websocket, "strategies.refresh")
                await self._handle_server_event(msg)
        except json.JSONDecodeError:
            logger.exception("Invalid JSON received while refreshing parsing strategies")
        except Exception:
            logger.exception("Failed to refresh parsing strategies from SaaS")
