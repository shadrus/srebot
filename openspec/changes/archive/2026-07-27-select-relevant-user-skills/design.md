## Context

The control plane currently resolves active skills by scope and user-authored regex trigger rules,
then appends every match in full to the main system prompt. A missing trigger rule is treated as a
match, so organization skills commonly appear in unrelated metric, log, or project analyses and
are replayed in every LLM tool round.

Users should describe skills in domain language rather than configure orchestration internals.
The existing `description` field already provides a compact, user-authored statement and can be
relabelled as “When to use” without introducing a new column.

## Goals / Non-Goals

**Goals:**

- Select zero to three relevant skills for every alert, follow-up, or general analysis.
- Keep organization and bot scope enforcement deterministic.
- Preserve existing trigger rules as a deterministic pre-filter.
- Keep full skill content out of the selector request.
- Count selector usage as part of the incident's billed LLM usage.
- Preserve existing skill descriptions and content.

**Non-Goals:**

- Selecting skills for alert extraction or SODA discovery.
- Adding new user-configurable routing fields, modes, capabilities, or MCP tool patterns.
- Automatically rewriting non-empty existing descriptions.
- Guaranteeing that a selected skill can complete every workflow when an external MCP is
  unavailable.

## Decisions

### Use a dedicated compact LLM selector

The orchestrator will query active skills by scope, apply existing trigger rules, and send the
remaining candidates to a separate selector completion before building the main system prompt. Its
catalog contains only skill ID, name, description, and scope; request context contains the question
or primary alert summary plus compact available-tool names and descriptions. The selector returns
JSON containing at most three IDs and may return an empty list.

This avoids adding more user-authored routing metadata and avoids exposing a `load_skill` tool to
the main model, which would consume a diagnostic tool round and complicate the existing WebSocket
protocol.

### Fail closed on selector errors

Malformed JSON, unknown IDs, an API failure, or an empty selection will activate no skills. The
main analysis still proceeds with its normal system prompt, so a selector failure does not fail the
incident or encourage irrelevant prompt injection. Selected IDs are intersected with the
scope- and trigger-filtered candidate set, deduplicated, and capped at three in candidate order.

A malformed response with `finish_reason=length` is treated as a recoverable budget exhaustion:
retry the same compact request once with a larger configured completion budget. All other malformed
responses fail closed immediately, and a failed or truncated retry also fails closed. Include usage
from every attempted completion in incident token accounting.

### Reuse `description` as “When to use”

No new routing-description column will be added. Existing descriptions remain unchanged, new API
validation requires a non-blank description, and the selector falls back to the skill name for any
legacy blank value. The frontend changes only the label, placeholder, and help text to guide future
authors toward describing activation context.

### Preserve trigger rules as a pre-filter

The `trigger_rules` database column, API property, resolver behavior, form controls, and catalog
display remain unchanged. A skill whose rules do not match is excluded before the selector request;
a skill without rules remains eligible and the LLM decides whether its description is relevant.
This preserves existing configuration while preventing catch-all skills from automatically adding
their full content to every main prompt.

### Account for selection usage

Selector prompt and completion token usage will be added to the current incident counters before
the main analysis begins. The selector exchange is not added to `conversation_history`; only the
selected full skill block appears in the persisted system prompt. Logs record candidate and
selected skill names without logging full skill contents.

## Risks / Trade-offs

- [Selector adds one LLM request when candidates exist] → Keep the catalog compact, skip the call
  when no candidates exist, and cap the response to a small JSON object.
- [Selector chooses an irrelevant skill] → Explicitly permit zero selections, cap at three, require
  direct relevance, and validate returned IDs.
- [Selector fails or returns malformed output] → Fail closed and continue the analysis without
  skills; retry once first only when an unparseable response explicitly ended due to length.
- [Descriptions written as generic summaries route poorly] → Relabel the field as “When to use”
  with concrete localized guidance; preserve existing content for manual refinement.
- [A broad trigger still admits an irrelevant candidate] → The selector applies direct-relevance
  filtering after trigger matching and may choose zero skills.

## Migration Plan

1. Deploy backend selector behavior without a database migration.
2. Deploy the frontend with the revised description label while preserving trigger controls.
3. Existing skills participate using their current descriptions after their current trigger rules
   match.
4. Rollback requires only redeploying the previous frontend/backend versions.

## Open Questions

None. The maximum selection count is fixed at three, and an empty selection is valid.
