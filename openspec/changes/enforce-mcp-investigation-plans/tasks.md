## 1. Shared Incident Routing

- [x] 1.1 Add one tested helper that resolves allowed MCP server names from a validated primary
  `Alert` and current server conditions.
- [x] 1.2 Replace initial-analysis inline routing with the shared resolver without changing the
  advertised schema for valid alerts.
- [x] 1.3 Reconstruct and validate the saved primary alert for incident follow-ups, derive a concrete
  allowed-server list, and fail closed to an empty list for invalid context.
- [x] 1.4 Keep general queries explicitly unscoped while ensuring incident callers cannot reach that
  `None` fallback accidentally.

## 2. Local Execution Authorization

- [x] 2.1 Add a schema utility that extracts an immutable exact-name allowlist from the tools
  advertised for one WebSocket request.
- [x] 2.2 Pass the request allowlist through alert and follow-up execution loops into batch tool
  execution.
- [x] 2.3 Reject unadvertised names before `call_tool`, return structured per-call errors, and preserve
  successful authorized calls in mixed batches.

## 3. Verification

- [x] 3.1 Add routing tests for matching and nonmatching servers, configuration changes, empty saved
  alerts, malformed saved alerts, and intentionally unscoped general queries.
- [x] 3.2 Add WebSocket execution tests proving exact advertised calls execute, registered but
  unadvertised calls do not reach the MCP executor, snapshots remain stable, and mixed batches
  retain call IDs.
- [x] 3.3 Run Ruff checks/formatting and the complete bot test suite, then validate the OpenSpec
  change and complete the mandatory independent compliance review.
