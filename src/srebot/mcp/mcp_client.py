"""MCP client for connecting to external MCP servers."""

import json
import logging
import re
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


def _fix_date_histogram_intervals(data: Any) -> Any:
    if isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            if k == "date_histogram" and isinstance(v, dict):
                new_v = dict(v)
                if "calendar_interval" in new_v:
                    val = new_v["calendar_interval"]
                    if isinstance(val, str):
                        val_strip = val.strip().lower()
                        valid_words = {"minute", "hour", "day", "week", "month", "quarter", "year"}
                        is_valid = False
                        if val_strip in valid_words:
                            is_valid = True
                        else:
                            m = re.match(r"^(\d+)([a-zA-Z]+)$", val_strip)
                            if m:
                                num = int(m.group(1))
                                unit = m.group(2)
                                if num == 1 and unit in {"m", "h", "d", "w", "q", "y"}:
                                    is_valid = True
                        if not is_valid:
                            new_v["fixed_interval"] = new_v.pop("calendar_interval")
                new_dict[k] = _fix_date_histogram_intervals(new_v)
            else:
                new_dict[k] = _fix_date_histogram_intervals(v)
        return new_dict
    elif isinstance(data, list):
        return [_fix_date_histogram_intervals(item) for item in data]
    else:
        return data


class ExternalMCPClient:
    """
    Connects to external MCP servers via SSE or Streamable HTTP.
    Provides a bridge to the internal tool registry.
    Uses AsyncExitStack for safe lifecycle management.
    """

    def __init__(self, url: str, transport: str = "sse"):
        self.url = url
        self.transport = transport
        self._session: ClientSession | None = None
        self._exit_stack = AsyncExitStack()

    async def connect(self):
        """Establish connection to the MCP server via SSE or Streamable HTTP."""
        if self._session:
            return

        logger.info("Connecting to external MCP server (%s): %s", self.transport, self.url)

        if self.transport == "http":
            # streamablehttp_client returns (read_stream, write_stream, _)
            read, write, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(self.url)
            )
        else:
            # sse_client returns (read_stream, write_stream)
            read, write = await self._exit_stack.enter_async_context(sse_client(self.url))

        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        logger.info("MCP server initialized: %s", self.url)

    async def get_tools_as_openai_schema(self) -> list[dict]:
        """Fetch tools from the MCP server and convert them to OpenAI tool schemas."""
        if not self._session:
            await self.connect()

        result = await self._session.list_tools()
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                },
            }
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool on the external MCP server."""
        if name == "search" and isinstance(arguments, dict) and "query_body" in arguments:
            arguments = dict(arguments)
            arguments["query_body"] = _fix_date_histogram_intervals(arguments["query_body"])

        if not self._session:
            await self.connect()

        try:
            result = await self._session.call_tool(name, arguments)
            # MCP results can have multiple components (text, image, resource)
            texts = [c.text for c in result.content if hasattr(c, "text")]
            content = "\n".join(texts)

            if getattr(result, "isError", False):
                return json.dumps({"error": content or "Unknown tool error"})

            return content
        except Exception as exc:
            error_msg = str(exc)
            if not error_msg:
                error_msg = type(exc).__name__
            logger.exception("Error calling external MCP tool %s", name)
            return json.dumps({"error": error_msg})

    async def close(self):
        """Close the connection and session."""
        await self._exit_stack.aclose()
        self._session = None
