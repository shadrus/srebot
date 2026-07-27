---
name: test-new-feature
description: Execute evidence-based testing for newly implemented or changed functionality, including a mandatory real end-to-end request through the feature's public entry point whenever a runnable test environment exists. Use when asked to verify, test, smoke-test, regression-test, or validate a feature through unit tests, APIs, databases, workers, LLMs, MCP tools, messaging integrations, or browser UI. Determine the applicable layers from the feature and repository instead of assuming a fixed suite; never report PASS when the required real E2E path was skipped.
---

# Test New Feature

Verify a feature through the smallest sufficient set of realistic checks. Adapt the test plan to
the change; do not require Telegram, MCP, LLM, browser, or database checks unless the feature
actually crosses those boundaries.

## Enforce the Default Execution Contract

Treat invocation of this skill as a request to execute tests, not merely inspect code or propose a
test plan. When a runnable local or explicitly approved test environment exists:

1. Send at least one real request through the same public entry point used by a user or client.
2. Let that request traverse every affected in-scope process and service without mocking those
   boundaries.
3. Verify the final user-visible result and important persisted side effects.
4. Capture a unique run marker plus sanitized request, response, and correlation evidence.

Existing automated tests, direct function calls, mocked networks, synthetic events, and database
inserts are supporting evidence; none substitutes for this real E2E request. Do not stop after
unit or integration tests while the runnable public path remains available.

If a real E2E request needs missing credentials, unavailable infrastructure, unsafe production
access, or authorization for an external side effect, ask for the minimum missing input. If it
remains unavailable, mark E2E as `NOT EXECUTED`, state the exact boundary reached, and use
`BLOCKED` or `PASS WITH CAVEATS`; never use `PASS`.

## Establish the Test Contract

1. Read the user request, acceptance criteria, relevant specification, and repository instructions.
2. Inspect the implementation diff and nearby tests without disturbing unrelated working-tree
   changes.
3. Translate each promised behavior into an observable assertion.
4. Identify affected layers and important boundaries using
   [test-matrix.md](references/test-matrix.md).
5. Record assumptions. Ask the user only when missing information would materially change the
   test environment, external side effects, or success criteria.

Treat repository instructions and explicit user constraints as authoritative. Testing does not
authorize product fixes, production changes, broad data deletion, or messages to third parties.

## Build a Proportional Plan

Select checks by risk and evidence value:

- Start with focused existing tests nearest to the change.
- Add boundary tests where data changes representation or ownership.
- Add one realistic happy-path test through the public entry point.
- Add negative, fail-closed, permission, or cleanup cases when failure could be costly.
- Run broader regression suites only when the affected surface or repository conventions justify
  their cost.

Always execute the mandatory real E2E request. Add other browser, live-integration, or external
service scenarios only when they prove a separate behavior or risk. Distinguish tests that were
executed from checks inferred by inspection.

## Prepare Safely

1. Inspect service and dependency status before starting anything.
2. Reuse healthy local dependencies. Do not restart databases, queues, MCP servers, or other shared
   infrastructure unless required and authorized.
3. Load secrets inside the process from the approved environment or configuration. Never print
   secret-bearing files, URLs, headers, connection strings, or command arguments.
4. Reduce third-party client logging when it may expose credentials. Report variable names and
   availability, not values.
5. Generate a unique run marker for all temporary records, messages, users, files, and resources.
6. Record pre-existing failures and dirty files so they are not attributed to the feature.

Prefer reversible setup. Resolve exact cleanup targets from recorded IDs and markers; never use a
broad pattern or unverified environment variable for deletion.

## Execute and Capture Evidence

Run the focused checks first and stop expanding a failing path until the failure is understood.
For every check, capture the action, expected result, actual result, and compact evidence such as a
test name, response status, record ID, screenshot path, or sanitized log excerpt.

After focused diagnostics pass, execute the real E2E request before declaring the run complete.

### Code and API

- Follow the repository's package manager, formatter, linter, and test commands.
- Mock external networks in unit tests.
- For integration checks, validate response content and persisted side effects, not only status
  codes.
- Exercise malformed, empty, unauthorized, and boundary inputs when relevant.

### Browser UI

- Enter through a normal public page, then use visible controls for internal navigation.
- Perform real clicks and typing; never force actions.
- Assert each transition and the resulting state.
- Check persistence with a reload or a second read when the feature saves data.
- Capture screenshots only when they add evidence.

### Messaging and Asynchronous Flows

- Verify enqueue/receipt, processing, persistence, and user-visible output separately.
- Prefer an approved test account or chat and uniquely marked messages.
- If a platform cannot inject a genuine inbound event, state the limitation and combine the
  closest supported real transport check with a synthetic handler event. Never describe that as a
  fully real end-to-end test and never give the overall run an unconditional `PASS`.
- Wait on observable state with bounded polling; avoid arbitrary long sleeps.

### LLM and MCP

- Inspect the actual prompt/tool schemas and the actual tool-call arguments, not only the final
  answer.
- Check that irrelevant context is excluded and tool parameters match the runtime schema.
- Flag malformed requests, repeated equivalent calls, speculative labels or identifiers, retries
  without new information, and calls made after sufficient evidence already existed.
- Record model calls, MCP calls by tool, failures, retries, and token usage when available.
- Keep raw reasoning private; report observable decisions, requests, responses, and efficiency
  findings.

### Data and Side Effects

- Verify records through the owning API when possible and directly in storage when persistence is
  part of the contract.
- Check both intended writes and absence of unintended writes.
- Validate tenant, user, permission, and idempotency boundaries when relevant.

## Diagnose Without Expanding Scope

Classify failures as:

- `FEATURE`: implementation violates the test contract.
- `TEST`: harness, assertion, fixture, or timing is incorrect.
- `ENVIRONMENT`: unavailable or unhealthy dependency prevents a valid observation.
- `PRE-EXISTING`: reproducible outside the feature change.
- `INCONCLUSIVE`: evidence is insufficient to assign another class.

Re-run the smallest failing check once after correcting a confirmed test or transient environment
issue. Do not modify product code unless the user also asked for fixes.

## Clean Up and Verify

Delete only artifacts created by the run, using recorded identifiers. Preserve audit artifacts the
user asked to keep. Stop only processes started by the run, then verify that pre-existing
dependencies remain in their original state. Report anything intentionally left behind.

## Report

Use [report-template.md](references/report-template.md). Lead with the overall result, list
requirements and their evidence, separate defects from environment limitations, and identify
untested risk. Never claim full E2E coverage when any required boundary was mocked or synthetic.

Finish with a concise recommendation: ready, ready with caveats, or not ready.

Before reporting `PASS`, confirm all of the following:

- a real request entered through the public entry point;
- all affected in-scope runtime boundaries were traversed without mocks;
- the final result and required side effects were observed;
- evidence contains a unique marker or correlation identifier;
- cleanup was completed or retained artifacts were disclosed.
