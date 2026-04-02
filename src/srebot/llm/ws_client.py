import asyncio
import json
import logging
from typing import Any

from websockets.asyncio.client import connect

from srebot.parser.alert_parser import update_remote_strategies

logger = logging.getLogger(__name__)


class SaaSWSClient:
    def __init__(self, ws_url: str, token: str) -> None:
        self.ws_url = ws_url
        self.token = token

    async def _handle_server_event(self, event_data: dict) -> bool:
        """Handle non-request-specific events like strategy updates. Returns True if handled."""
        event = event_data.get("event")
        if event == "update_strategies":
            strategies = event_data.get("strategies", [])
            update_remote_strategies(strategies)
            return True
        return False

    async def analyze_alert(
        self, alert_data: dict[str, Any], tools_schema: list[dict[str, Any]], tool_executor: Any
    ) -> str:
        url = f"{self.ws_url}?token={self.token}"
        try:
            logger.info("Connecting to SaaS Control Plane at %s...", self.ws_url)
            async with asyncio.timeout(600):  # 10m total analysis limit
                async with connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=10
                ) as websocket:
                    # 1. Wait for initial strategies (always sent by server on connect)
                    # and then send the initial alert data
                    while True:
                        raw = await websocket.recv()
                        msg = json.loads(raw)
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
                    }
                    await websocket.send(json.dumps(payload))

                    # 2. Loop to handle Server Events
                    used_tools: set[str] = set()
                    while True:
                        response_raw = await websocket.recv()
                        response = json.loads(response_raw)
                        event = response.get("event")

                        if event == "final_analysis":
                            content = response.get("text", "")
                            if used_tools:
                                tools_str = ", ".join(
                                    f"<code>{t}</code>" for t in sorted(used_tools)
                                )
                                content += f"\n\n<b>🛠 Tools used:</b> {tools_str}"
                            return content

                        elif event == "execute_tools":
                            tools = response.get("tools", [])

                            async def run_tool(tc: dict) -> dict:
                                t_id = tc.get("tool_call_id")
                                t_name = tc.get("tool_name")
                                t_args = tc.get("args", {})

                                logger.info("SaaS requested tool execution: %s", t_name)
                                used_tools.add(t_name)

                                try:
                                    args_str = (
                                        json.dumps(t_args) if isinstance(t_args, dict) else t_args
                                    )
                                    result = await asyncio.wait_for(
                                        tool_executor(t_name, args_str), timeout=60
                                    )
                                    result_str = str(result)
                                except TimeoutError:
                                    logger.error("Tool %s timed out after 60s", t_name)
                                    result_str = "Error: Tool execution timed out after 60s"
                                except Exception as e:
                                    logger.error("Tool %s failed: %s", t_name, e)
                                    result_str = f"Error: {e}"

                                return {"tool_call_id": t_id, "data": result_str}

                            results = await asyncio.gather(*(run_tool(tc) for tc in tools))
                            result_payload = {"event": "tools_result", "results": results}
                            await websocket.send(json.dumps(result_payload))

                        elif event == "error":
                            msg = response.get("message")
                            logger.error("SaaS Error: %s", msg)
                            return f"⚠️ Control Plane Error: {msg}"

                        elif await self._handle_server_event(response):
                            continue

                        else:
                            logger.warning("Unknown event from SaaS: %s", event)
        except TimeoutError:
            logger.error("Analysis timed out after 10 minutes")
            return (
                "⚠️ <b>Analysis timed out:</b> The AI took too long to respond. "
                "Please investigate manually."
            )
        except Exception as e:
            logger.error("WebSocket connection to SaaS failed: %s", e)
            return f"⚠️ Failed to connect to AI Control Plane: {e}"

    async def extract_alerts(self, text: str) -> list[dict[str, Any]]:
        """Request the SaaS Control Plane to parse raw text into structured Alert objects."""
        url = f"{self.ws_url}?token={self.token}"
        try:
            logger.info("Connecting to SaaS Control Plane for smart parsing...")
            async with connect(url) as websocket:
                # Wait for strategies
                while True:
                    raw = await websocket.recv()
                    msg = json.loads(raw)
                    if await self._handle_server_event(msg):
                        break

                payload = {
                    "event": "extract_alerts",
                    "text": text,
                }
                await websocket.send(json.dumps(payload))

                while True:
                    response_raw = await websocket.recv()
                    response = json.loads(response_raw)
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
        except Exception as e:
            logger.error("Smart parsing via SaaS failed: %s", e)
            return []

    async def refresh_strategies(self) -> None:
        """Connect briefly to the SaaS Control Plane to fetch the latest parsing strategies."""
        url = f"{self.ws_url}?token={self.token}"
        try:
            logger.info("Connecting to SaaS Control Plane to refresh parsing strategies...")
            async with connect(url) as websocket:
                # The server sends 'update_strategies' immediately after accept
                raw = await websocket.recv()
                msg = json.loads(raw)
                await self._handle_server_event(msg)
        except Exception as e:
            logger.error("Failed to refresh parsing strategies from SaaS: %s", e)
