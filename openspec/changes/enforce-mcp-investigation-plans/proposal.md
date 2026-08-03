## Why

Incident tool routing is scoped for the initial alert analysis but follow-up requests can currently
fall back to every registered MCP tool, and the local agent executes Control Plane requests without
revalidating them against the catalog advertised for that analysis. This weakens cluster isolation
exactly when an engineer asks for additional evidence such as logs after a metrics-based RCA.

## What Changes

- Derive one request-scoped MCP authorization snapshot for every alert and incident follow-up from
  the original alert context and configured server conditions.
- Recompute the incident follow-up scope from saved `alert_data` instead of treating an omitted
  server list as permission to use every registered server.
- Bind each WebSocket analysis loop to the exact prefixed tool names advertised in its tool schema.
- Reject unadvertised tool requests locally and return a structured tool error without invoking an
  MCP server.
- Preserve intentionally unscoped general-query behavior and the existing MCP WebSocket event
  contract.
- Coordinate with the Control Plane change of the same name, which adds investigation planning and
  completion enforcement without expanding local tool authority.

## Capabilities

### New Capabilities

- `request-scoped-mcp-authorization`: Scope incident MCP catalogs consistently across initial and
  follow-up analyses and enforce the resulting allowlist at local execution time.

### Modified Capabilities

None.

## Impact

- Affects MCP routing in `llm/agent.py`, shared follow-up context handling, WebSocket tool execution,
  and related unit tests.
- Uses existing alert data, server conditions, prefixed tool names, and OpenAI-compatible schemas;
  no custom external MCP metadata is required.
- Does not change external MCP servers or the existing `execute_tools` / `tools_result` protocol.
- Must be deployed compatibly with, but remains a security boundary independent from, the
  `srebot-backend` investigation-planning change.
