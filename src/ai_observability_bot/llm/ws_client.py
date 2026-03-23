"""WebSocket Client to communicate with SaaS Control Plane."""
import json
import logging
from typing import Any

from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)

class SaaSWSClient:
    def __init__(self, ws_url: str, token: str) -> None:
        self.ws_url = ws_url
        self.token = token
        
    async def analyze_alert(
        self, 
        alert_data: dict[str, Any], 
        tools_schema: list[dict[str, Any]], 
        tool_executor: Any
    ) -> str:
        url = f"{self.ws_url}?token={self.token}"
        try:
            logger.info("Connecting to SaaS Control Plane at %s...", self.ws_url)
            async with connect(url) as websocket:
                # 1. Send the initial alert data
                payload = {
                    "event": "analyze_alert",
                    "alert_data": alert_data,
                    "tools": tools_schema
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
                            tools_str = ", ".join(f"<code>{t}</code>" for t in sorted(used_tools))
                            content += f"\n\n<b>🛠 Tools used:</b> {tools_str}"
                        return content
                        
                    elif event == "execute_tool":
                        tool_call_id = response.get("tool_call_id")
                        tool_name = response.get("tool_name")
                        args = response.get("args", {})
                        
                        logger.info("SaaS requested tool execution: %s", tool_name)
                        used_tools.add(tool_name)
                        
                        try:
                            # executor is a callback that runs the local MCP tool
                            # dict is usually stringified for call_tool
                            args_str = json.dumps(args) if isinstance(args, dict) else args
                            result = await tool_executor(tool_name, args_str)
                            result_str = str(result)
                        except Exception as e:
                            logger.error("Tool %s failed: %s", tool_name, e)
                            result_str = f"Error: {e}"
                            
                        # Send back the result
                        result_payload = {
                            "event": "tool_result",
                            "tool_call_id": tool_call_id,
                            "data": result_str
                        }
                        await websocket.send(json.dumps(result_payload))
                        
                    elif event == "error":
                        msg = response.get("message")
                        logger.error("SaaS Error: %s", msg)
                        return f"⚠️ Control Plane Error: {msg}"
                        
                    else:
                        logger.warning("Unknown event from SaaS: %s", event)
                        
        except Exception as e:
            logger.error("WebSocket connection to SaaS failed: %s", e)
            return f"⚠️ Failed to connect to AI Control Plane: {e}"
