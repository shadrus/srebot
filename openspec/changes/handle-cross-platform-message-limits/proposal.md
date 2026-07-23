## Why

Long SRE analyses and bursts of follow-up activity are handled inconsistently across Time,
Telegram, Slack, and Discord. Discord alone splits oversized responses, while the other
integrations can reject or truncate them, and the current conversation limits can be exhausted by
one engineer for an entire incident.

## What Changes

- Add platform-aware delivery that splits long formatted responses without losing content or
  producing invalid platform markup.
- Preserve message order, expose partial-delivery outcomes, and associate every delivered chunk
  with the originating incident so replies to any chunk retain context.
- Handle platform API throttling with bounded, ordered retries that respect server-provided retry
  delays and avoid unsafe duplicate sends.
- Separate per-user cooldowns from per-user and per-incident conversation quotas, with
  platform-scoped identities and quota consumption only for accepted follow-up requests.
- Apply the same delivery and quota semantics to Time, Telegram, Slack, and Discord while retaining
  platform-specific size limits and formatting.

## Capabilities

### New Capabilities

- `cross-platform-message-delivery`: Platform-aware pagination, ordered multi-message delivery,
  delivery receipts, incident-context registration, and transport rate-limit handling.
- `conversation-quotas`: Platform-scoped user cooldowns and configurable per-user/per-incident
  follow-up limits with consistent rejection behavior.

### Modified Capabilities

None.

## Impact

- Affects shared bot workflows and all four platform adapters under `src/srebot/bot/`.
- Changes Redis keys and state operations used for follow-up cooldown and turn accounting.
- May require enabling or configuring rate-limit support in the Telegram and Slack client
  libraries.
- Adds unit tests for semantic chunking, multi-message context registration, partial failures,
  ordering, retries, and quota isolation.
