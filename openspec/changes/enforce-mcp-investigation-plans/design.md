## Context

External MCP tools are registered once, prefixed with their configured server name, and exposed as
OpenAI-compatible schemas. Initial alert analysis filters those schemas by each server's configured
condition, but incident follow-up handlers do not pass an allowed-server list, so the agent's
`None` fallback exposes every registered tool. The WebSocket client then executes any prefixed name
requested by the Control Plane if it exists in the global registry.

The paired `srebot-backend` change will plan and gate investigations, but the local agent remains
the trust boundary for cluster isolation and MCP execution. It must enforce authority independently
of Control Plane prompting or planning and must remain compatible with unmodified external MCP
servers.

## Goals / Non-Goals

**Goals:**

- Apply the same server-condition routing to initial and incident follow-up requests.
- Fail closed when saved incident context cannot produce a valid alert scope.
- Bind tool execution to the exact schema advertised for the active WebSocket request.
- Return ordinary structured tool failures for rejected calls so partial analysis can continue.
- Keep routing vendor-neutral and compatible with existing MCP schemas and transports.

**Non-Goals:**

- Build the investigation plan or decide which diagnostic signal should be queried next.
- Add custom metadata requirements to external MCP servers.
- Change intentionally unscoped general-query behavior.
- Replace existing read-only name filtering or Control Plane argument validation.
- Introduce a new WebSocket event or protocol version.

## Decisions

### Centralize incident server resolution from an Alert

Add one routing helper that accepts a validated primary `Alert` and returns configured server names
whose condition is absent or matches that alert. Initial analysis will call it with its primary
object. Incident follow-up will reconstruct the primary `Alert` from saved `alert_data` and call the
same helper before asking the registry for schemas.

If the saved list is empty or the first item cannot be validated as an `Alert`, the incident
follow-up receives an empty tool schema rather than `None`. This preserves conversational access to
the prior RCA while making technical diagnostics fail closed.

Alternatives considered:

- Persist the original allowed-server list. This can become stale when configuration changes and
  duplicates derivable state.
- Let every platform handler calculate routing. That repeats security logic and risks platform
  drift.

### Distinguish unscoped general queries from scoped incident requests

`None` will retain its explicit meaning only for general queries: all registered tools are
advertised because no incident scope exists. Incident analysis paths will always pass a concrete
list, including an empty list when no server is authorized.

This avoids a breaking change to general diagnostics while eliminating ambiguous `None` behavior
from incident flows.

### Derive the local execution allowlist from the advertised schema

At the start of each alert or follow-up WebSocket loop, extract the exact prefixed function names
from `tools_schema` into an immutable set. Pass that set to the batch executor. Before invoking
`call_tool`, reject any requested name absent from the set and return a serialized error associated
with the original `tool_call_id`.

The schema is already the request's authority snapshot, so no new catalog identifier or wire field
is needed. Registry changes cannot expand an in-flight request because only names present in the
snapshot are executable.

Alternatives considered:

- Trust backend schema validation. That makes the remote Control Plane the only security boundary.
- Check only the server prefix. Exact-name validation is stricter and already available at no
  compatibility cost.

### Preserve batch failure semantics

Unauthorized calls will be represented as tool errors in the existing `tools_result` batch. Other
authorized calls in the same batch continue concurrently, failure callbacks remain usable, and the
final response can identify unavailable sources. No exception will abort the entire analysis merely
because one requested tool is outside the snapshot.

## Risks / Trade-offs

- [Saved legacy alert context is malformed] → Advertise no incident tools and report unavailable
  diagnostics rather than widening authority.
- [Configuration changes during a long incident] → Recompute server conditions on each follow-up;
  the new configuration deliberately governs new turns while in-flight snapshots remain stable.
- [A legitimate backend call is rejected after schema drift] → Bind execution and backend planning
  to the same per-request schema; retry only in a new analysis turn after reconnecting.
- [General queries remain broad] → Keep this existing behavior explicit and isolated from incident
  paths; broader general-query scoping can be specified separately.

## Migration Plan

1. Deploy local allowlist validation; it is compatible with current Control Plane events.
2. Deploy shared incident routing for initial and follow-up paths.
3. Deploy the paired Control Plane planning change in either order; rejected out-of-scope calls are
   ordinary tool failures during mixed-version rollout.
4. Roll back by restoring the previous agent version; no data migration is required.

## Open Questions

None. General-query multi-cluster selection remains intentionally outside this change.
