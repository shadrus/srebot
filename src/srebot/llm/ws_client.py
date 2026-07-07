import asyncio
import json
import logging
from typing import Any

from websockets.asyncio.client import connect

from srebot.parser.alert_parser import update_remote_strategies

logger = logging.getLogger(__name__)

_MAX_TOOL_RESULT_CHARS = 8000


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
        try:
            logger.info("Connecting to SaaS Control Plane at %s...", self.ws_url)
            async with asyncio.timeout(600):  # 10m total analysis limit
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
                    used_tools: set[str] = set()
                    while True:
                        response = await _recv_ws_json(websocket, "alert.loop")
                        event = response.get("event")

                        if event == "final_analysis":
                            content = response.get("text", "")
                            incident_id = response.get("incident_id")
                            if used_tools:
                                tools_str = ", ".join(
                                    f"<code>{t}</code>" for t in sorted(used_tools)
                                )
                                content += f"\n\n<b>🛠 Tools used:</b> {tools_str}"
                            return content, incident_id

                        elif event == "execute_tools":
                            tools = response.get("tools", [])

                            async def run_tool(tc: dict[str, Any]) -> dict[str, Any]:
                                t_id = str(tc.get("tool_call_id", ""))
                                t_name = str(tc.get("tool_name", ""))
                                t_args = tc.get("args", {})

                                logger.info("SaaS requested tool execution: %s", t_name)
                                if t_name:
                                    used_tools.add(t_name)

                                try:
                                    args_str = (
                                        json.dumps(t_args) if isinstance(t_args, dict) else t_args
                                    )
                                    result = await asyncio.wait_for(
                                        tool_executor(t_name, args_str), timeout=60
                                    )
                                    result_str = _trim_tool_result(str(result))
                                except TimeoutError:
                                    logger.error("Tool %s timed out after 60s", t_name)
                                    result_str = "Error: Tool execution timed out after 60s"
                                except Exception as exc:
                                    logger.exception("Tool %s failed", t_name)
                                    result_str = f"Error: {exc}"

                                return {"tool_call_id": t_id, "data": result_str}

                            results = await asyncio.gather(*(run_tool(tc) for tc in tools))
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
            logger.error("Analysis timed out after 10 minutes")
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

        Returns:
            The bot's follow-up answer as a string.
        """
        try:
            logger.info("Connecting to SaaS Control Plane for follow-up analysis...")
            async with asyncio.timeout(300):  # 5m — simpler than full RCA
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

                    used_tools: set[str] = set()
                    while True:
                        response = await _recv_ws_json(websocket, "followup.loop")
                        event = response.get("event")

                        if event == "final_analysis":
                            content = response.get("text", "")
                            incident_id = response.get("incident_id")
                            if used_tools:
                                tools_str = ", ".join(
                                    f"<code>{t}</code>" for t in sorted(used_tools)
                                )
                                content += f"\n\n<b>🛠 Tools used:</b> {tools_str}"
                            return content, incident_id

                        elif event == "execute_tools":
                            tools = response.get("tools", [])

                            async def run_tool(tc: dict) -> dict:
                                t_id = str(tc.get("tool_call_id", ""))
                                t_name = str(tc.get("tool_name", ""))
                                t_args = tc.get("args", {})

                                logger.info("SaaS requested tool execution (follow-up): %s", t_name)
                                if t_name:
                                    used_tools.add(t_name)

                                try:
                                    from srebot.mcp.registry import call_tool

                                    args_str = (
                                        json.dumps(t_args) if isinstance(t_args, dict) else t_args
                                    )
                                    result = await asyncio.wait_for(
                                        call_tool(t_name, args_str), timeout=60
                                    )
                                    result_str = _trim_tool_result(str(result))
                                except TimeoutError:
                                    logger.error("Tool %s timed out after 60s", t_name)
                                    result_str = "Error: Tool execution timed out after 60s"
                                except Exception as exc:
                                    logger.exception("Tool %s failed", t_name)
                                    result_str = f"Error: {exc}"

                                return {"tool_call_id": t_id, "data": result_str}

                            results = await asyncio.gather(*(run_tool(tc) for tc in tools))
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
            logger.error("Follow-up analysis timed out after 5 minutes")
            return (
                "⚠️ <b>Analysis timed out:</b> The AI took too long to respond. Please try again.",
                None,
            )
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
