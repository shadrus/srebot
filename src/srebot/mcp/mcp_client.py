"""MCP client for connecting to external MCP servers."""

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

_RECOVERABLE_TRANSPORT_ERRORS = (ClosedResourceError, BrokenResourceError, EndOfStream)
_TOOL_CALL_TIMEOUT = 55


@dataclass(slots=True)
class _ClientRequest:
    """One operation executed by the MCP connection owner task."""

    operation: str
    future: asyncio.Future[Any]
    name: str = ""
    arguments: dict | None = None


def _is_recoverable_transport_error(exc: BaseException) -> bool:
    """Return whether an exception or nested exception group is a transport failure."""
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_recoverable_transport_error(nested) for nested in exc.exceptions)
    return isinstance(exc, _RECOVERABLE_TRANSPORT_ERRORS)


def _contains_cancellation(exc: BaseException) -> bool:
    """Return whether an exception or nested exception group contains cancellation."""
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_cancellation(nested) for nested in exc.exceptions)
    return isinstance(exc, asyncio.CancelledError)


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
        self._request_queue: asyncio.Queue[_ClientRequest] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._worker_lock = asyncio.Lock()
        self._closed = False

    async def connect(self):
        """Establish connection to the MCP server via SSE or Streamable HTTP."""
        await self._submit("connect")

    async def _connect_session(self) -> None:
        """Open the MCP transport inside the connection owner task."""
        if self._session:
            return

        logger.info("Connecting to external MCP server (%s): %s", self.transport, self.url)

        try:
            if self.transport == "http":
                # streamablehttp_client returns (read_stream, write_stream, _)
                read, write, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(self.url)
                )
            else:
                # sse_client returns (read_stream, write_stream)
                # MCP's five-minute default is an inactivity timeout, not a request timeout.
                # Keep persistent SSE sessions open while idle; individual tool calls remain
                # bounded by _TOOL_CALL_TIMEOUT and reset the session when they time out.
                read, write = await self._exit_stack.enter_async_context(
                    sse_client(self.url, sse_read_timeout=None)
                )

            self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        except BaseException:
            await self._reset_session()
            raise
        logger.info("MCP server initialized: %s", self.url)

    async def get_tools_as_openai_schema(self) -> list[dict]:
        """Fetch tools from the MCP server and convert them to OpenAI tool schemas."""
        return await self._submit("list_tools")

    async def _get_tools_as_openai_schema(self) -> list[dict]:
        """Fetch tool schemas inside the connection owner task."""
        if not self._session:
            await self._connect_session()

        try:
            async with asyncio.timeout(_TOOL_CALL_TIMEOUT):
                result = await self._session.list_tools()
        except BaseException as exc:
            if _contains_cancellation(exc):
                raise
            if isinstance(exc, TimeoutError) or _is_recoverable_transport_error(exc):
                await self._reset_session()
            raise
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

        return await self._submit("call_tool", name=name, arguments=arguments)

    async def _call_tool_managed(self, name: str, arguments: dict) -> str:
        """Call a tool and reconnect once, entirely inside the owner task."""
        if not self._session:
            try:
                await self._connect_session()
            except BaseException as exc:
                await self._reset_session()
                if _contains_cancellation(exc):
                    raise
                return self._tool_error_response(name, exc)

        try:
            return await self._call_tool_once(name, arguments)
        except BaseException as exc:
            if _contains_cancellation(exc):
                raise
            if not isinstance(exc, TimeoutError) and not _is_recoverable_transport_error(exc):
                return self._tool_error_response(name, exc)

            logger.warning(
                "External MCP transport closed while calling tool %s; reconnecting once",
                name,
                exc_info=True,
            )
            await self._reset_session()

            try:
                await self._connect_session()
                return await self._call_tool_once(name, arguments)
            except BaseException as retry_exc:
                if _contains_cancellation(retry_exc):
                    raise
                if isinstance(retry_exc, TimeoutError) or _is_recoverable_transport_error(
                    retry_exc
                ):
                    await self._reset_session()
                return self._tool_error_response(name, retry_exc)

    async def _call_tool_once(self, name: str, arguments: dict) -> str:
        """
        Call a tool once on the current session.

        Args:
            name: External MCP tool name.
            arguments: Tool arguments.

        Returns:
            Tool text content, or a serialized JSON error for MCP-level tool failures.
        """
        if not self._session:
            raise RuntimeError("MCP session is not connected")

        async with asyncio.timeout(_TOOL_CALL_TIMEOUT):
            result = await self._session.call_tool(name, arguments)
        # MCP results can have multiple components (text, image, resource)
        texts = [c.text for c in result.content if hasattr(c, "text")]
        content = "\n".join(texts)

        if getattr(result, "isError", False):
            return json.dumps({"error": content or "Unknown tool error"})

        return content

    def _tool_error_response(self, name: str, exc: BaseException) -> str:
        """
        Convert a tool-call exception into the JSON error envelope expected by callers.

        Args:
            name: External MCP tool name.
            exc: Exception raised by the client or transport.

        Returns:
            Serialized JSON error object.
        """
        error_msg = str(exc)
        if not error_msg:
            error_msg = type(exc).__name__
        logger.exception("Error calling external MCP tool %s", name)
        return json.dumps({"error": error_msg})

    async def _reset_session(self) -> None:
        """Drop the current MCP session and prepare a fresh exit stack for reconnect."""
        exit_stack = self._exit_stack
        self._session = None
        self._exit_stack = AsyncExitStack()
        try:
            await exit_stack.aclose()
        except BaseException as exc:
            if _contains_cancellation(exc):
                raise
            logger.debug("Ignoring MCP session close failure before reconnect", exc_info=True)

    async def _submit(
        self,
        operation: str,
        name: str = "",
        arguments: dict | None = None,
    ) -> Any:
        """Submit an operation to the task that owns the MCP cancel scopes."""
        await self._ensure_worker()
        async with self._worker_lock:
            if self._closed:
                raise RuntimeError("MCP client is closed")
            if not self._request_queue or not self._worker_task or self._worker_task.done():
                raise RuntimeError("MCP worker is not available")
            future = asyncio.get_running_loop().create_future()
            self._request_queue.put_nowait(_ClientRequest(operation, future, name, arguments))
        return await asyncio.shield(future)

    async def _ensure_worker(self) -> None:
        """Start the connection owner task once for this client."""
        async with self._worker_lock:
            if self._closed:
                raise RuntimeError("MCP client is closed")
            if self._worker_task and not self._worker_task.done():
                return
            self._request_queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(
                self._run_worker(),
                name=f"mcp-client:{self.url}",
            )

    async def _run_worker(self) -> None:
        """Own all MCP contexts and execute serialized client operations."""
        if not self._request_queue:
            raise RuntimeError("MCP worker started without a request queue")

        try:
            while True:
                request = await self._request_queue.get()
                try:
                    if request.operation == "connect":
                        result = await self._connect_session()
                    elif request.operation == "list_tools":
                        result = await self._get_tools_as_openai_schema()
                    elif request.operation == "call_tool":
                        result = await self._call_tool_managed(
                            request.name,
                            request.arguments or {},
                        )
                    else:
                        raise ValueError(f"Unknown MCP client operation: {request.operation}")
                except BaseException as exc:
                    if not request.future.done():
                        if _contains_cancellation(exc):
                            request.future.cancel()
                        else:
                            request.future.set_exception(exc)
                    if _contains_cancellation(exc):
                        raise
                else:
                    if not request.future.done():
                        request.future.set_result(result)
        finally:
            try:
                await self._reset_session()
            finally:
                while not self._request_queue.empty():
                    pending = self._request_queue.get_nowait()
                    if not pending.future.done():
                        pending.future.cancel()

    async def close(self) -> None:
        """Cancel the owner task, close its MCP contexts there, and wait for exit."""
        async with self._worker_lock:
            self._closed = True
            worker = self._worker_task
            if not worker or worker.done():
                if worker and worker.done() and not worker.cancelled():
                    worker.exception()
                self._worker_task = None
                self._request_queue = None
                return
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            self._worker_task = None
            self._request_queue = None
