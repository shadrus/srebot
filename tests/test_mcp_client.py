import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import ClosedResourceError

from srebot.mcp.mcp_client import ExternalMCPClient, ExternalMCPClientPool


def _mock_tool_result(text: str, is_error: bool = False) -> MagicMock:
    mock_content = MagicMock()
    mock_content.text = text

    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_result.isError = is_error
    return mock_result


@pytest.mark.asyncio
async def test_call_tool_handles_exception_with_empty_str():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    # Simulate an exception with an empty string representation
    class EmptyException(Exception):
        def __str__(self):
            return ""

    client._session.call_tool.side_effect = EmptyException()

    result = await client.call_tool("some_tool", {})

    # It now returns the exception class name if message is empty
    assert json.loads(result) == {"error": "EmptyException"}
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_handles_is_error_flag_in_result():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    # Simulate a result with isError=True and some error text in content.
    client._session.call_tool.return_value = _mock_tool_result("Internal Server Error", True)

    result = await client.call_tool("some_tool", {})

    # It now returns a JSON error even if content is just text
    assert json.loads(result) == {"error": "Internal Server Error"}
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_rewrites_calendar_interval_for_elasticsearch_search():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    arguments = {
        "index": "filebeat-*",
        "query_body": {
            "aggs": {
                "by_time": {"date_histogram": {"field": "@timestamp", "calendar_interval": "15m"}},
                "by_day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "1d"}},
            }
        },
    }

    # Simulate a successful tool response
    client._session.call_tool.return_value = _mock_tool_result("success")

    await client.call_tool("search", arguments)

    # Verify that client._session.call_tool was called with rewritten arguments
    called_args = client._session.call_tool.call_args[0]
    passed_name = called_args[0]
    passed_arguments = called_args[1]

    by_time_hist = passed_arguments["query_body"]["aggs"]["by_time"]["date_histogram"]
    by_day_hist = passed_arguments["query_body"]["aggs"]["by_day"]["date_histogram"]

    assert passed_name == "search"
    assert by_time_hist["fixed_interval"] == "15m"
    assert "calendar_interval" not in by_time_hist
    assert by_day_hist["calendar_interval"] == "1d"
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_reconnects_once_after_closed_resource():
    client = ExternalMCPClient("dummy_cmd")
    initial_session = AsyncMock()
    retry_session = AsyncMock()
    client._session = initial_session

    initial_session.call_tool.side_effect = ClosedResourceError()
    retry_session.call_tool.return_value = _mock_tool_result("success after reconnect")

    async def reconnect():
        client._session = retry_session

    client._connect_session = AsyncMock(side_effect=reconnect)

    result = await client.call_tool("some_tool", {"arg": "value"})

    assert result == "success after reconnect"
    initial_session.call_tool.assert_awaited_once_with("some_tool", {"arg": "value"})
    retry_session.call_tool.assert_awaited_once_with("some_tool", {"arg": "value"})
    client._connect_session.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_returns_error_when_reconnect_retry_fails():
    client = ExternalMCPClient("dummy_cmd")
    initial_session = AsyncMock()
    retry_session = AsyncMock()
    client._session = initial_session

    initial_session.call_tool.side_effect = ClosedResourceError()
    retry_session.call_tool.side_effect = ClosedResourceError()

    async def reconnect():
        client._session = retry_session

    client._connect_session = AsyncMock(side_effect=reconnect)

    result = await client.call_tool("some_tool", {})

    assert json.loads(result) == {"error": "ClosedResourceError"}
    assert client._session is None
    initial_session.call_tool.assert_awaited_once_with("some_tool", {})
    retry_session.call_tool.assert_awaited_once_with("some_tool", {})
    client._connect_session.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_timeout_ends_the_complete_operation_without_retry(monkeypatch):
    monkeypatch.setattr("srebot.mcp.mcp_client._TOOL_CALL_TIMEOUT", 0.01)
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()

    async def slow_call(*_args):
        await asyncio.sleep(1)

    client._session.call_tool.side_effect = slow_call
    client._connect_session = AsyncMock()

    result = await client.call_tool("slow_tool", {})

    assert json.loads(result) == {"error": "TimeoutError"}
    client._connect_session.assert_not_awaited()
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_serializes_parallel_calls_on_single_client():
    client = ExternalMCPClient("dummy_cmd")

    class BlockingSession:
        def __init__(self):
            self.active_calls = 0
            self.max_active_calls = 0

        async def call_tool(self, name: str, arguments: dict):
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            await asyncio.sleep(0.01)
            self.active_calls -= 1
            return _mock_tool_result(f"{name}:{arguments['i']}")

    session = BlockingSession()
    client._session = session

    results = await asyncio.gather(
        client.call_tool("some_tool", {"i": 1}),
        client.call_tool("some_tool", {"i": 2}),
    )

    assert sorted(results) == ["some_tool:1", "some_tool:2"]
    assert session.max_active_calls == 1
    await client.close()


@pytest.mark.asyncio
async def test_pool_runs_calls_in_parallel_without_sharing_sessions(monkeypatch):
    release_calls = asyncio.Event()
    all_calls_started = asyncio.Event()
    sessions = []
    active_calls = 0
    max_active_calls = 0

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            self.active_calls = 0
            self.max_active_calls = 0
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, name: str, _arguments: dict):
            nonlocal active_calls, max_active_calls
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            if active_calls == 2:
                all_calls_started.set()
            await release_calls.wait()
            active_calls -= 1
            self.active_calls -= 1
            return _mock_tool_result(name)

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    pool = ExternalMCPClientPool("http://mcp.example/sse", size=2)
    await pool.connect()

    first = asyncio.create_task(pool.call_tool("first", {}))
    second = asyncio.create_task(pool.call_tool("second", {}))
    await asyncio.wait_for(all_calls_started.wait(), timeout=0.1)
    release_calls.set()

    assert sorted(await asyncio.gather(first, second)) == ["first", "second"]
    assert max_active_calls == 2
    assert all(session.max_active_calls == 1 for session in sessions)
    await pool.close()


@pytest.mark.asyncio
async def test_pool_keeps_cancelled_call_borrowed_until_client_finishes(monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, name: str, _arguments: dict):
            if name == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
            return _mock_tool_result(name)

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    pool = ExternalMCPClientPool("http://mcp.example/sse", size=1)
    await pool.connect()

    first = asyncio.create_task(pool.call_tool("first", {}))
    await first_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(pool.call_tool("second", {}))
    await asyncio.sleep(0.01)
    assert not second_started.is_set()

    release_first.set()
    assert await second == "second"
    await pool.close()


@pytest.mark.asyncio
async def test_pool_connect_is_atomic_when_one_member_fails(monkeypatch):
    opened_transports = 0
    closed_transports = 0
    initialized_sessions = 0

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        nonlocal opened_transports, closed_transports
        opened_transports += 1
        try:
            yield object(), object()
        finally:
            closed_transports += 1

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            nonlocal initialized_sessions
            initialized_sessions += 1
            if initialized_sessions == 2:
                raise RuntimeError("connection rejected")

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    pool = ExternalMCPClientPool("http://mcp.example/sse", size=2)

    with pytest.raises(RuntimeError, match="connection rejected"):
        await pool.connect()

    assert opened_transports == 2
    assert closed_transports == 2
    with pytest.raises(RuntimeError, match="pool is closed"):
        await pool.call_tool("query", {})


@pytest.mark.asyncio
async def test_pool_shutdown_cancels_active_calls_and_rejects_waiters(monkeypatch):
    active_started = asyncio.Event()

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, _name: str, _arguments: dict):
            active_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    pool = ExternalMCPClientPool("http://mcp.example/sse", size=1)
    await pool.connect()

    active = asyncio.create_task(pool.call_tool("active", {}))
    await active_started.wait()
    waiting = asyncio.create_task(pool.call_tool("waiting", {}))
    await asyncio.sleep(0)
    await pool.close()
    await pool.close()

    results = await asyncio.gather(active, waiting, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], RuntimeError)


@pytest.mark.asyncio
async def test_pool_rejects_calls_started_after_shutdown(monkeypatch):
    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    pool = ExternalMCPClientPool("http://mcp.example/sse")
    await pool.connect()
    await pool.close()

    with pytest.raises(RuntimeError, match="pool is closed"):
        await asyncio.wait_for(pool.call_tool("late", {}), timeout=0.1)


@pytest.mark.asyncio
async def test_transport_is_opened_used_and_closed_by_same_owner_task(monkeypatch):
    task_ids: dict[str, int] = {}
    transport_options: dict[str, object] = {}

    @asynccontextmanager
    async def fake_sse_client(_url: str, **kwargs):
        transport_options.update(kwargs)
        task_ids["transport_enter"] = id(asyncio.current_task())
        try:
            yield object(), object()
        finally:
            task_ids["transport_exit"] = id(asyncio.current_task())

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            task_ids["session_enter"] = id(asyncio.current_task())
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            task_ids["session_exit"] = id(asyncio.current_task())

        async def initialize(self):
            pass

        async def call_tool(self, _name: str, _arguments: dict):
            task_ids["tool_call"] = id(asyncio.current_task())
            return _mock_tool_result("success")

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    caller_task_id = id(asyncio.current_task())
    client = ExternalMCPClient("http://mcp.example/sse")

    await client.connect()
    assert await client.call_tool("some_tool", {}) == "success"
    await client.close()

    assert len(set(task_ids.values())) == 1
    assert task_ids["transport_enter"] != caller_task_id
    assert transport_options["sse_read_timeout"] is None


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_owner_task_or_break_shutdown(monkeypatch):
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    owner_task_ids: list[int] = []

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        owner_task_ids.append(id(asyncio.current_task()))
        try:
            yield object(), object()
        finally:
            owner_task_ids.append(id(asyncio.current_task()))

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, _name: str, _arguments: dict):
            tool_started.set()
            await release_tool.wait()
            return _mock_tool_result("late success")

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    client = ExternalMCPClient("http://mcp.example/sse")
    await client.connect()

    caller = asyncio.create_task(client.call_tool("slow_tool", {}))
    await tool_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    await client.close()

    assert len(owner_task_ids) == 2
    assert owner_task_ids[0] == owner_task_ids[1]


@pytest.mark.asyncio
async def test_shutdown_cancels_active_and_queued_calls(monkeypatch):
    tool_started = asyncio.Event()

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        yield object(), object()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, _name: str, _arguments: dict):
            tool_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    client = ExternalMCPClient("http://mcp.example/sse")
    await client.connect()

    active = asyncio.create_task(client.call_tool("active", {}))
    await tool_started.wait()
    queued = asyncio.create_task(client.call_tool("queued", {}))
    await asyncio.sleep(0)
    await client.close()

    results = await asyncio.gather(active, queued, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_submit_during_close_is_rejected_without_hanging(monkeypatch):
    transport_closing = asyncio.Event()
    allow_transport_close = asyncio.Event()

    @asynccontextmanager
    async def fake_sse_client(_url: str, **_kwargs):
        try:
            yield object(), object()
        finally:
            transport_closing.set()
            await allow_transport_close.wait()

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            pass

        async def initialize(self):
            pass

    monkeypatch.setattr("srebot.mcp.mcp_client.sse_client", fake_sse_client)
    monkeypatch.setattr("srebot.mcp.mcp_client.ClientSession", FakeSession)
    client = ExternalMCPClient("http://mcp.example/sse")
    await client.connect()

    closing = asyncio.create_task(client.close())
    await transport_closing.wait()
    submitting = asyncio.create_task(client.call_tool("late", {}))
    await asyncio.sleep(0)
    assert not submitting.done()

    allow_transport_close.set()
    await closing
    with pytest.raises(RuntimeError, match="MCP client is closed"):
        await asyncio.wait_for(submitting, timeout=0.1)


@pytest.mark.asyncio
async def test_list_tools_transport_failure_resets_dead_session():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()
    client._session.list_tools.side_effect = ClosedResourceError()

    with pytest.raises(ClosedResourceError):
        await client.get_tools_as_openai_schema()

    assert client._session is None
    await client.close()


@pytest.mark.asyncio
async def test_exception_group_with_transport_failure_reconnects():
    client = ExternalMCPClient("dummy_cmd")
    initial_session = AsyncMock()
    retry_session = AsyncMock()
    client._session = initial_session
    initial_session.call_tool.side_effect = ExceptionGroup(
        "transport failed",
        [ClosedResourceError()],
    )
    retry_session.call_tool.return_value = _mock_tool_result("success after grouped error")

    async def reconnect():
        client._session = retry_session

    client._connect_session = AsyncMock(side_effect=reconnect)

    result = await client.call_tool("some_tool", {})

    assert result == "success after grouped error"
    client._connect_session.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
async def test_reset_session_propagates_cancellation_and_clears_references():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()
    client._exit_stack = AsyncMock()
    client._exit_stack.aclose.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await client._reset_session()

    assert client._session is None
    assert isinstance(client._exit_stack, AsyncExitStack)


@pytest.mark.asyncio
async def test_reset_session_propagates_nested_cancellation():
    client = ExternalMCPClient("dummy_cmd")
    client._session = AsyncMock()
    client._exit_stack = AsyncMock()
    cancellation_group = BaseExceptionGroup("cleanup", [asyncio.CancelledError()])
    client._exit_stack.aclose.side_effect = cancellation_group

    with pytest.raises(BaseExceptionGroup) as raised:
        await client._reset_session()

    assert raised.value is cancellation_group
    assert client._session is None
