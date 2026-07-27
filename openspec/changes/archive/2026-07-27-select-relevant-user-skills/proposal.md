## Why

Organization skills are currently injected in full whenever their trigger rules match, and a
skill without trigger rules matches every analysis. This adds irrelevant instructions and repeated
prompt tokens. Existing trigger rules must remain available, but adding more routing fields such as
modes or MCP capabilities would make skill creation too technical.

## What Changes

- Add an automatic skill-selection step before the main analysis that chooses zero to three
  relevant skills from a compact catalog.
- Reuse the existing skill `description` as the user-facing “When to use” text; do not add a new
  database field.
- Give the selector only skill IDs, names, descriptions, request context, and compact summaries of
  available MCP tools. Full skill instructions are loaded only after selection.
- Allow the selector to choose no skill and treat three as a maximum rather than a target.
- Preserve existing trigger-rule configuration and use it as a deterministic candidate pre-filter.
- Preserve organization and bot assignment scopes as deterministic access boundaries.

## Capabilities

### New Capabilities

- `contextual-skill-selection`: Automatically select zero to three relevant user-authored skills
  for alert, follow-up, and general analyses before constructing the main LLM prompt.

### Modified Capabilities

None.

## Impact

- Backend resolver, prompt construction, LLM orchestration, and tests.
- Frontend skill form should present `description` as “When to use” while retaining existing
  trigger-rule controls.
- One small selector LLM request is added per analysis when eligible skills exist; its usage must be
  included in incident token accounting.
- Existing skill names, descriptions, contents, trigger rules, scopes, activation state, and bot
  assignments are preserved.
