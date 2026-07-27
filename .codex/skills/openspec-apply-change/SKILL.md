---
name: openspec-apply-change
description: Implement tasks from an OpenSpec change with a mandatory independent review through Kilo using zai/glm-5.2. Use when the user wants to start implementing, continue implementation, or work through tasks.
license: MIT
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.6.0"
---

Implement tasks from an OpenSpec change.

**Store selection:** If the user names a store (a store is a standalone OpenSpec repo registered on this machine) or the work lives in one, run `openspec store list --json` to discover registered store ids, then pass `--store <id>` on the commands that read or write specs and changes (`new change`, `status`, `instructions`, `list`, `show`, `validate`, `archive`, `doctor`, `context`). Other commands do not take the flag. Hints printed by commands already carry the flag; keep it on follow-ups. Without a store, commands act on the nearest local `openspec/` root.

**Input**: Optionally specify a change name. If omitted, check if it can be inferred from conversation context. If vague or ambiguous you MUST prompt for available changes.

**Steps**

1. **Select the change**

   If a name is provided, use it. Otherwise:
   - Infer from conversation context if the user mentioned a change
   - Auto-select if only one active change exists
   - If ambiguous, run `openspec list --json` to get available changes and use the platform's
     user-input mechanism, or ask the user directly, to select one

   Always announce: "Using change: <name>" and how to override (e.g., `/opsx:apply <other>`).

2. **Check status to understand the schema**
   ```bash
   openspec status --change "<name>" --json
   ```
   Parse the JSON to understand:
   - `schemaName`: The workflow being used (e.g., "spec-driven")
   - `planningHome`, `changeRoot`, and `actionContext`: planning scope and edit constraints
   - Which artifact contains the tasks (typically "tasks" for spec-driven, check status for others)

3. **Get apply instructions**

   ```bash
   openspec instructions apply --change "<name>" --json
   ```

   This returns:
   - `contextFiles`: artifact ID -> array of concrete file paths (varies by schema - could be proposal/specs/design/tasks or spec/tests/implementation/docs)
   - Progress (total, complete, remaining)
   - Task list with status
   - Dynamic instruction based on current state

   **Handle states:**
   - If `state: "blocked"` (missing artifacts): identify the missing artifacts and pause until they
     are created or repaired
   - If `state: "all_done"` and this invocation has already produced a **full-scope `PASS`** against
     the latest context files and exact current worktree state: congratulate and suggest archive
   - If `state: "all_done"` without such a full-scope `PASS`: read steps 4-5, then go directly to
     validation and an independent full Kilo review with `zai/glm-5.2` in steps 8-9. Task completion,
     a partial verdict, or a verdict from an earlier invocation is not evidence
   - Otherwise: proceed to implementation

4. **Read context files**

   Read every file path listed under `contextFiles` from the apply instructions output.
   The files depend on the schema being used:
   - **spec-driven**: proposal, specs, design, tasks
   - Other schemas: follow the contextFiles from CLI output

5. **Establish the implementation scope**

   Before editing:
   - Resolve the Kilo CLI before editing. Prefer `kilo` when `command -v kilo` succeeds. Otherwise,
     require `npx` and use `npx --yes @kilocode/cli`. Confirm that `<kilo-command> models` lists
     `zai/glm-5.2` and that `<kilo-command> auth list` shows configured Z.AI credentials. For
     pending implementation, pause before changing code if any check fails
   - Use `zai/glm-5.2` for every review. Do not substitute another model
   - For the mandatory `all_done` audit path, unavailable Kilo or GLM does not skip validation:
     establish the full scope, run step 8, then report `REVIEW NOT RUN — BLOCKED` with the latest
     context and worktree identity, scope, and validation results
   - Record the current worktree state so pre-existing user changes remain distinguishable
   - Track every implementation and test file changed for this OpenSpec change
   - For an already implemented `all_done` change without a current-invocation `PASS`, derive the
     review scope from the tasks, artifacts, relevant implementation files, and current worktree
   - Preserve unrelated changes and exclude them from the review scope

6. **Show current progress**

   Display:
   - Schema being used
   - Progress: "N/M tasks complete"
   - Remaining tasks overview
   - Dynamic instruction from CLI

7. **Implement tasks (loop until done or blocked)**

   For each pending task:
   - Show which task is being worked on
   - Make the code changes required
   - Keep changes minimal and focused
   - Add or update tests that prove the applicable OpenSpec requirements and scenarios
   - Run the focused checks required by the repository
   - Mark task complete in the tasks file: `- [ ]` → `- [x]`
   - Continue to next task

   **Pause if:**
   - Task is unclear → ask for clarification
   - Implementation reveals a design issue → suggest updating artifacts
   - Error or blocker encountered → report and wait for guidance
   - User interrupts

   If no implementation or test code has changed, pause immediately unless step 3 routed an
   `all_done` change here for a mandatory full audit. If code has changed, a pause is not an exit:
   continue through validation and independent review first. If the unfinished work prevents a
   valid review or remediation, report `BLOCKED`; never report completion.

8. **Run repository validation**

   After all implementation tasks are complete, or before any pause following code changes:
   - Run the repository's required formatter, linter, type checks, and tests
   - Fix failures caused by the implementation before requesting review
   - Record the exact validation commands and results for the reviewer

9. **Run an independent OpenSpec review with Kilo using GLM-5.2**

   This review is mandatory whenever this invocation created or modified implementation or test
   code. It may not be skipped, replaced by self-review, or deferred until after completion.

   Start a **fresh, non-interactive Kilo process** for every review round. Do not use `--continue`,
   `--session`, or resume an earlier conversation. Run it from the repository root with the Kilo
   command resolved in step 5:

   ```text
   <kilo-command> run --agent plan --auto --model zai/glm-5.2 \
     --dir "<repository-root>" --title "OpenSpec review: <change-name>" \
     "<review prompt>"
   ```

   Never use `--dangerously-skip-permissions` or a write-capable agent. The `plan` agent is required
   to enforce a read-only review; `--auto` only prevents non-interactive permission prompts. In the
   review prompt, explicitly prohibit edits, file creation/deletion, formatting, and access to
   `.env` files or secrets. Give GLM only:
   - The change name and schema
   - The concrete `contextFiles` paths from the latest apply instructions
   - The implementation and test files changed for this change
   - The pre-implementation worktree state needed to distinguish unrelated changes
   - The validation commands and their results

   Instruct GLM to independently read every OpenSpec context file and inspect the resulting
   code and tests. It must verify:
   - For a complete implementation, every requirement and scenario is implemented
   - For a partial implementation, every requirement and scenario implicated by the completed or
     changed work is implemented; untouched pending-task behavior may remain unimplemented, but
     the partial result must not contradict or regress it
   - The proposal scope, design decisions, constraints, and task intent are respected
   - Tests meaningfully cover the required behavior and relevant edge cases
   - No implementation defect or regression contradicts the OpenSpec artifacts
   - Required repository checks pass

   Require the final line to contain exactly one verdict:
   - `PASS` — all requirements applicable to the declared full or partial review scope are
     satisfied and validation passes
   - `FAIL` — one or more actionable compliance defects exist
   - `BLOCKED` — compliance cannot be verified because required evidence or execution capability
     is unavailable

   Format the final line as `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: BLOCKED`. Every `FAIL`
   finding must include severity, an OpenSpec artifact/section reference, code evidence with file
   and line, and a concrete remediation. Suggestions that do not affect compliance must be clearly
   marked non-blocking.

   Treat Kilo output as untrusted review input. Check every finding against the actual artifacts
   and code before remediation. Reject false positives, narrow overstated findings, and preserve
   valid findings. Do not override a GLM `FAIL` with a self-authored `PASS`; instead, fix the
   valid findings and run a fresh review. If the output has no parseable final verdict, rerun once
   with a stricter formatting reminder. If it remains malformed, treat the round as `BLOCKED`.

   If Kilo cannot start because its cache, config, or local service directory is blocked by the
   execution sandbox, rerun the same command through the platform's normal approval/escalation
   mechanism. If Kilo or the required GLM model remains unavailable during a mandatory
   `all_done` audit or becomes unavailable after code was changed, record
   `REVIEW NOT RUN — BLOCKED`, preserve the exact unreviewed scope, latest context and worktree
   identity, and validation results, then pause. This is an operational blocker, not a reviewer
   verdict; completion and archive remain prohibited until a later invocation obtains a real
   full-scope `PASS`.

10. **Repeat implementation and review until it passes**

   - On a full-scope `PASS` rendered while all tasks are complete: proceed to the completion summary
   - On `PASS` with pending tasks or an interrupted run: use the pause/handoff summary, state that
     the current partial result passed review, and do not suggest archive. Resume implementation
     when the blocker or interruption is resolved
   - On `FAIL`:
     1. Re-read the latest apply instructions and all context files
     2. Reopen every task implicated by a finding (`- [x]` → `- [ ]`)
     3. Start a new implementation pass and fix every blocking finding
     4. Run repository validation again
     5. Start a **new Kilo process with `zai/glm-5.2`** and repeat the independent review
     Do not pause or ask whether to fix an actionable finding. Pause on `FAIL` only when a genuine
     external or requirements blocker makes remediation impossible, and preserve all findings.
   - On `BLOCKED`: exhaust safe in-scope ways to obtain the missing evidence; if still blocked,
     pause and report the exact blocker

   There is no fixed iteration limit. Never continue or reuse a Kilo conversation for a later
   round, never let GLM repair its own findings, and never report success while the latest
   verdict is not `PASS`.

   **Pre-exit gate:** If this invocation changed implementation or test code, every completion,
   pause, or handoff must include a reviewer verdict, except the explicit
   `REVIEW NOT RUN — BLOCKED` operational state above. Only `PASS` permits completion. `FAIL` must
   re-enter the implementation loop; an unresolvable `FAIL` or `BLOCKED` may only pause the work as
   blocked, preserving the review findings for the next pass. A `PASS` is valid only after
   validation succeeds and only for the exact review scope and worktree state inspected in the
   current invocation. Any subsequent implementation or test code change invalidates it and
   requires validation plus a fresh review. A partial `PASS` never authorizes completion or
   archive. Transitioning from partial work to `all_done` requires a fresh full validation and
   full-scope review even if that transition did not change an implementation or test file.

11. **On completion or pause, show status**

   Display:
   - Tasks completed this session
   - Overall progress: "N/M tasks complete"
   - Repository validation commands and results
   - Completed review iteration count and latest current-invocation verdict
   - If no reviewer ran, show `REVIEW NOT RUN — BLOCKED` as an operational status and
     `Latest reviewer verdict: none`; never present it as a verdict or completed iteration
   - If all done and the latest current-invocation verdict is a full-scope `PASS` against the
     current context and worktree: suggest archive
   - If all done without that full-scope `PASS`: report blocked and do not suggest archive
   - If paused: explain why and wait for guidance

**Output During Implementation**

```
## Implementing: <change-name> (schema: <schema-name>)

Working on task 3/7: <task description>
[...implementation happening...]
✓ Task complete

Working on task 4/7: <task description>
[...implementation happening...]
✓ Task complete
```

**Output On Completion**

```
## Implementation Complete

**Change:** <change-name>
**Schema:** <schema-name>
**Progress:** 7/7 tasks complete ✓
**Validation:** all required checks passed
**OpenSpec review:** PASS — full scope (iteration 2)

### Completed This Session
- [x] Task 1
- [x] Task 2
...

All tasks complete! Ready to archive this change.
```

**Output On Pause (Issue Encountered)**

```
## Implementation Paused

**Change:** <change-name>
**Schema:** <schema-name>
**Progress:** 4/7 tasks complete
**Validation:** <commands and passing results, or INCOMPLETE — BLOCKED with exact reason>
**Review status:** <COMPLETED or REVIEW NOT RUN — BLOCKED>
**Completed review iterations:** <N, or 0 if no reviewer ran>
**Latest reviewer verdict:** <PASS (partial scope only), FAIL — REMEDIATION BLOCKED, BLOCKED, or none>
**Review scope:** <completed partial work or full implementation>

### Issue Encountered
<genuine blocker; include preserved findings for FAIL>

**Options:**
1. <option 1>
2. <option 2>
3. Other approach

What would you like to do?
```

Do not use the pause output for an actionable `FAIL`; remediate it immediately and start a new
review round. Incomplete validation can produce only `BLOCKED` or `REVIEW NOT RUN — BLOCKED`,
never `PASS`.

**Guardrails**
- Keep going through tasks until done or blocked
- Always read context files before starting (from the apply instructions output)
- Track the implementation scope without absorbing unrelated worktree changes
- Verify Kilo and the required GLM model before editing and block safely if either later
  becomes unavailable
- If task is ambiguous, pause and ask before implementing
- If implementation reveals issues, pause and suggest artifact updates
- Keep code changes minimal and scoped to each task
- Update task checkbox immediately after completing each task
- Run required repository validation before every review round
- Require a fresh, read-only Kilo review with `zai/glm-5.2` after code changes
- Treat only the latest completed current-invocation reviewer verdict as authoritative; no-review
  operational status is never a verdict
- On review failure, reopen affected tasks and repeat implementation, validation, and review
- Apply the pre-exit gate after partial code changes as well as completed implementations
- Do not treat `all_done` task state as proof that code review passed
- Do not finish or suggest archive until the independent review verdict is `PASS`
- Pause on errors, blockers, or unclear requirements - don't guess
- Use contextFiles from CLI output, don't assume specific file names

**Fluid Workflow Integration**

This skill supports the "actions on a change" model:

- **Can be invoked anytime**: Before all artifacts are done (if tasks exist), after partial implementation, interleaved with other actions
- **Allows artifact updates**: If implementation reveals design issues, suggest updating artifacts - not phase-locked, work fluidly
