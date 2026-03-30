"""MCP client for connecting to external MCP servers."""

import json
import logging
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class ExternalMCPClient:
    """
    Connects to external MCP servers and provides a bridge to the internal tool registry.
    Uses AsyncExitStack for safe lifecycle management.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.server_params = StdioServerParameters(
            command=command,
            args=args or [],
            env=env,
        )
        self._session: ClientSession | None = None
        self._exit_stack = AsyncExitStack()

    async def connect(self):
        """Establish connection to the MCP server."""
        if self._session:
            return

        logger.info("Connecting to external MCP server: %s", self.server_params.command)
        read, write = await self._exit_stack.enter_async_context(stdio_client(self.server_params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        logger.info("MCP server initialized: %s", self.server_params.command)

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
        except Exception as e:
            error_msg = str(e)
            if not error_msg:
                error_msg = type(e).__name__
            logger.error("Error calling external MCP tool %s: %s", name, error_msg)
            return json.dumps({"error": error_msg})

    async def close(self):
        """Close the connection and session."""
        await self._exit_stack.aclose()
        self._session = None
