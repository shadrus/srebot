# Pool connections per external MCP server

Each configured external MCP server has a fixed-size connection pool so concurrent analyses can
execute tool calls in parallel without sharing an MCP session. Every pool member remains an
independent `ExternalMCPClient` with one owner worker that serializes all operations on its
connection; parallel calls within one client remain prohibited because MCP transports use
task-bound anyio cancel scopes that can be corrupted by concurrent access and transport failure.

The pool size is configured per server as `pool_size`, defaults to `1` for backward compatibility,
and must be between `1` and `32`. At startup all members connect in parallel, tool schemas are read
once, and the pool is published to the registry only after every configured connection is ready.
Any connection or schema-registration failure closes the whole candidate pool and preserves the
existing fail-fast startup contract.

Tool calls borrow one member at a time from a FIFO pool and return it after the underlying client
operation actually finishes. A caller cancellation does not make a still-running client available.
There is no analysis affinity, fairness reservation, acquisition timeout, or queue-size limit;
the existing outer tool-execution timeout bounds each caller's total wait. The client's 55-second
deadline covers connect, reconnect, retry, and tool execution so active MCP work finishes before
the outer 60-second timeout under normal operation. Transport teardown still runs synchronously in
the connection owner task and is not forcibly abandoned: if teardown itself stalls, the caller may
reach its outer timeout while the pool correctly retains that member until cleanup completes.

Runtime MCP outages do not terminate the bot or permanently remove members. Failed calls return
tool errors, disconnected clients remain eligible for later borrowing, and they reconnect lazily on
subsequent calls. Shutdown rejects new borrowing, cancels waiters, and closes all pool members in
parallel. The first implementation uses lifecycle and wait-state logging without adding a circuit
breaker, background health supervisor, or pool metrics.
