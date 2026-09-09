# Dynamic progress messages

While the bot processes an alert, general query, or follow-up, it edits one temporary progress
message on Telegram, Slack, Discord, and Time Messenger. Progress communicates confirmed phases
and accepted actions without exposing tool traces or model reasoning.

## Behaviour

- Start with `Analyzing request`.
- When an accepted tool batch actually starts, show one model-authored public status describing
  the batch's action and immediate purpose.
- After the batch completes, show `Analyzing results`.
- When only part of a batch succeeds, show that some data is unavailable while analysis continues.
- Replace the progress message with the final answer or a terminal error.
- Do not retain progress history or emit heartbeat edits for a long-running phase.
- Deduplicate progress text and edit no more than once every two seconds.
- A progress-delivery failure must never interrupt analysis or create another placeholder.

## Public status contract

The backend may add `progress_text` to an `execute_tools` event. The same LLM turn that selects
the tools supplies the text; no extra model request is made. The field is optional so old agents
ignore it and new agents fall back safely when an old backend omits it.

A public status must be one plain-text line of at most 100 characters. It may describe only the
accepted action and its immediate purpose. It must not contain Markdown, URLs, code, tool names,
tool arguments, secrets, hypotheses, or reasoning. It may name an entity only when that entity was
already visible in the originating user message or alert. Invalid or missing text falls back to
`Retrieving additional data`.

The agent publishes the status only when it begins the accepted batch. Localized, deterministic
messages cover the initial phase, post-tool analysis, partial failure, and fallback. A fixed hourglass
prefix is added by the agent.

## Test seams

- Backend orchestration: an accepted tool-call response exposes a validated optional
  `progress_text` on `execute_tools`, while invalid text is omitted.
- Agent WebSocket client: execution emits the public status at batch start and deterministic
  phases after success or partial failure without changing the analysis result.
- Chat adapter delivery: all request types edit the existing placeholder, throttle and deduplicate
  progress, ignore progress-delivery failures, and preserve final replacement semantics.

