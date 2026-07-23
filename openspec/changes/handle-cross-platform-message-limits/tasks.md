## 1. Shared Message Pagination

- [x] 1.1 Add platform-neutral message constraints, chunk, delivery-status, and delivery-receipt
  value objects with validation and modern type hints.
- [x] 1.2 Implement the shared Markdown block tokenizer and conservative paginator with block, line,
  whitespace, and Unicode-grapheme fallbacks.
- [x] 1.3 Add valid close/reopen handling for oversized fenced code blocks and escaped plain-text
  fallback for indivisible inline constructs.
- [x] 1.4 Add focused paginator tests for fitting content, paragraphs, lists, tables, links,
  emphasis, fenced code, emoji, unbroken text, content preservation, and invalid limits.

## 2. Ordered Delivery and Receipts

- [x] 2.1 Implement a per-platform-channel delivery coordinator that serializes complete batches
  while allowing independent channels to progress concurrently.
- [x] 2.2 Implement placeholder-first delivery, continuation sending, fallback after edit failure,
  and complete/partial/failed receipt construction.
- [x] 2.3 Add coordinator tests for concurrent batches, non-interleaving order, independent channel
  progress, placeholder fallback, partial failure, and no replay of delivered chunks.

## 3. Conversation Quota State

- [x] 3.1 Add per-user and total-incident quota settings with validation, defaults of 5 and 20, and
  backward-compatible `followup_max_turns` alias behavior.
- [x] 3.2 Add scoped conversation identity construction from platform, chat, and user identifiers.
- [x] 3.3 Implement atomic Redis follow-up admission covering valid context, cooldown, per-user
  turns, total incident turns, TTLs, and structured rejection reasons.
- [x] 3.4 Update the shared follow-up workflow to validate context before admission and to apply
  scoped cooldown without incident turn counters for general queries.
- [x] 3.5 Add state and shared-workflow tests for identity isolation, expired/missing context,
  concurrent final slots, accepted attempts, cooldown rejection, both quota rejections, general
  queries, legacy configuration, and key expiry.

## 4. Shared Workflow Integration

- [x] 4.1 Change the `ChatAdapter` delivery contract and shared alert workflow to consume delivery
  receipts, use the primary ID as canonical, and register every successfully delivered chunk.
- [x] 4.2 Route analysis, follow-up, command, cooldown, and quota-rejection responses through the
  common pagination and delivery path.
- [x] 4.3 Add shared workflow tests for complete and partial multi-message context registration and
  replies resolving through continuation message IDs.

## 5. Platform Adapter Migration

- [x] 5.1 Migrate Discord to the shared paginator and receipt contract, retain the 1,900-character
  target, and remove the Discord-only splitter and `additional_message_ids` workaround.
- [x] 5.2 Migrate Telegram to per-chunk HTML rendering with a 3,800-visible-character target and
  independently valid HTML for every send and edit.
- [x] 5.3 Migrate Slack to per-chunk mrkdwn rendering with a 3,800-character target and threaded
  continuations.
- [x] 5.4 Fetch and validate Time `MaxPostSize` during startup, apply the safety reserve or 15,500
  fallback, and migrate Time sends and edits to the shared delivery contract.
- [x] 5.5 Add cross-platform adapter tests proving every chunk stays within its target, preserves
  content, remains valid for its formatting mode, and registers replies consistently.

## 6. Platform Rate-Limit Handling

- [x] 6.1 Enable Telegram rate limiting and add bounded `RetryAfter` tests without retrying
  ambiguous non-idempotent failures.
- [x] 6.2 Enable Slack's asynchronous rate-limit retry handler and test `Retry-After` handling and
  retry exhaustion.
- [x] 6.3 Add contract tests that preserve Discord.py bucket handling and Time idempotent retry
  behavior while mapping exhausted operations to partial or failed receipts.

## 7. Operations and Verification

- [x] 7.1 Add structured logs or metrics for chunk counts, delivery status, retry exhaustion,
  cooldown rejection, per-user quota rejection, and total-incident quota rejection.
- [x] 7.2 Document the new quota settings, legacy alias, platform delivery behavior, and Time limit
  discovery/fallback in configuration examples and the README.
- [x] 7.3 Run Ruff autofix and formatting over `src/` and `tests/`, then run the complete pytest
  suite and resolve all failures.
- [x] 7.4 Validate the OpenSpec change and confirm every delivery and quota scenario has automated
  test coverage.
