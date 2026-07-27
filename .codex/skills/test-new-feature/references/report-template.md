# Feature Test Report

## Verdict

`PASS`, `PASS WITH CAVEATS`, `FAIL`, or `BLOCKED` — one-sentence reason.

## Scope

- Feature or change tested
- Revision/diff and environment
- Explicit exclusions and assumptions

## Real E2E Evidence

- Public entry point and exact sanitized request
- Unique run marker or correlation identifier
- Runtime services and boundaries actually traversed
- Final response or user-visible result
- Persisted side effects and absence of unintended writes
- Mocks or synthetic boundaries, if any

If no real E2E request was sent, write `NOT EXECUTED` and the exact blocker. The overall verdict
must then be `BLOCKED` or `PASS WITH CAVEATS`, never `PASS`.

## Results

| Requirement or risk | Check | Result | Evidence |
|---|---|---|---|
| Observable behavior | Exact action/assertion | PASS/FAIL/NOT TESTED | Test, status, ID, sanitized log, or screenshot |

## Findings

For each finding include:

- severity and classification (`FEATURE`, `TEST`, `ENVIRONMENT`, `PRE-EXISTING`, `INCONCLUSIVE`);
- reproduction steps;
- expected versus actual behavior;
- compact sanitized evidence;
- affected users or risk.

## Efficiency

Include only when relevant:

- test duration and expensive stages;
- LLM model calls and token usage;
- MCP/tool calls grouped by tool;
- failed, redundant, or malformed calls.

## Cleanup

- Artifacts removed
- Artifacts intentionally retained
- Processes started and stopped
- Pre-existing services left unchanged

## Recommendation

State `ready`, `ready with caveats`, or `not ready`, followed by the smallest useful next action.
