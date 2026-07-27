## Context

The shared alert workflow produces one Markdown analysis and asks each `ChatAdapter` to display it.
Telegram converts the complete response to HTML, Slack converts it to mrkdwn, Time sends Markdown,
and Discord alone splits the response into 1,900-character chunks. The shared workflow records one
final message ID, so Discord needs an adapter-specific side channel to register continuation IDs.

Alert groups are processed concurrently. Without delivery coordination, introducing chunking on
the remaining platforms could interleave unrelated analyses and amplify platform rate limits.
Rate-limit behavior also differs by SDK: Discord handles API buckets internally, Time retries
idempotent creates, while the configured Telegram and Slack clients do not proactively handle
their available rate-limit mechanisms.

Follow-up admission currently sets a cooldown before validating incident context and counts turns
only by alert fingerprint. The five-turn limit is therefore shared by every participant in an
incident, identities are not namespaced by platform and chat, and general mentions have different
semantics from incident follow-ups.

## Goals / Non-Goals

**Goals:**

- Deliver every user-visible response within the selected platform's message-size constraints.
- Preserve response content and valid formatting across message boundaries.
- Keep all chunks from one delivery ordered and associate every chunk with incident context.
- Represent complete, partial, and failed delivery explicitly.
- Respect platform throttling without blind retries that can duplicate messages.
- Make follow-up cooldowns and turn quotas fair across users and isolated across platforms/chats.
- Preserve existing configuration behavior during migration.

**Non-Goals:**

- Limiting LLM token generation or organization-wide daily spend.
- Uploading overflow responses as files or linking to a separate report viewer.
- Running multiple chat integrations in one process.
- Providing exactly-once delivery when a platform returns an ambiguous network failure without an
  idempotency mechanism.
- Imposing an application-level maximum number of chunks; this change favors complete delivery.

## Decisions

### 1. Introduce a shared delivery model

Add platform-neutral value objects in the shared bot layer:

- `MessageConstraints` describes the target size, measurement strategy, and formatting mode.
- `MessageChunk` contains one independently valid platform-rendered part.
- `DeliveryReceipt` contains the primary message ID, every delivered message ID, delivered and
  expected chunk counts, and a `complete`, `partial`, or `failed` status.

`ChatAdapter.update_with_analysis` and shared follow-up response paths will return a
`DeliveryReceipt` rather than a single ID. The shared workflow will register every returned ID and
use the primary ID as the canonical alert reply.

This removes Discord's adapter-specific `additional_message_ids` path. A list of IDs alone was
considered, but rejected because it cannot distinguish an empty successful result from partial or
failed delivery.

### 2. Paginate canonical Markdown before platform rendering

Use a shared Markdown block tokenizer that recognizes paragraphs, headings, lists, blank-line
boundaries, fenced code blocks, and tables. It will greedily pack source blocks into logical pages,
then render each page independently for Telegram HTML, Slack mrkdwn, Discord Markdown, or Time
Markdown.

Split priority is:

1. block boundary;
2. line boundary inside a splittable block;
3. whitespace boundary;
4. Unicode grapheme boundary as the final fallback.

Fenced code blocks that exceed one page will be closed and reopened with the same language marker.
If an oversized inline construct cannot be divided while preserving its markup, that construct
will be emitted as escaped plain text rather than malformed markup. Delimiter whitespace can move
to an adjacent chunk, but visible source content must not be dropped.

Rendering the whole response and slicing the output was rejected because Telegram HTML tags and
Markdown fences can become unbalanced. Asking the LLM to produce shorter output was rejected
because model compliance is not a transport guarantee.

### 3. Keep platform constraints in adapters

The shared paginator accepts constraints supplied by each adapter:

- Telegram targets 3,800 visible characters, below the 4,096-character API limit after entity
  parsing.
- Slack targets 3,800 characters, below the documented 4,000-character recommendation.
- Discord retains the existing 1,900-character target below its 2,000-character limit.
- Time reads `MaxPostSize` from `/api/v4/config/client` during startup, subtracts a small rendering
  reserve, and falls back to 15,500 characters when the value is unavailable or invalid.

Limits are conservative targets rather than exact platform maxima so rendering escapes and future
continuation markers cannot push a request over the API limit. A single universal 1,900-character
limit was rejected because it would create unnecessary message floods on the other platforms.

### 4. Serialize whole deliveries per destination

Add a `DeliveryCoordinator` that queues complete delivery batches by platform and channel. One
batch holds the destination until its chunks finish, preventing unrelated analyses from
interleaving. Platform threads remain part of the delivery request, but Slack's channel-level rate
limit means the queue key cannot be thread-only.

The first chunk edits an existing analyzing placeholder when possible. Remaining chunks are sent
as continuations in the same thread or reply chain. If the edit fails, the adapter falls back to a
new first message and continues from that message.

Serializing individual chunks rather than batches was rejected because it could produce
`incident A part 1`, `incident B part 1`, `incident A part 2`, which is difficult to follow during
an incident. The trade-off is that a very long analysis delays later messages in the same channel.

### 5. Use platform-aware bounded retry behavior

The coordinator owns ordering and delivery state, while SDKs retain protocol-specific rate-limit
knowledge:

- configure Telegram's rate limiter and honor `RetryAfter`;
- enable Slack's async rate-limit retry handler and honor `Retry-After`;
- retain Discord.py's route/bucket handling;
- retain Time's idempotency keys and bounded retry transport.

The application will not add a generic retry around non-idempotent sends after connection
timeouts. It may retry explicit 429 rejections, idempotent Time creates, and edits where repeating
the same target/content is safe. Exhausted retries produce a partial or failed receipt; already
delivered chunks are not replayed.

### 6. Admit incident follow-ups atomically

Replace separate cooldown and turn-counter calls with one Redis-backed admission operation. It
will:

1. resolve and validate incident context;
2. build a scoped identity from platform, chat, and user;
3. atomically check the user cooldown, the user's incident turns, and total incident turns;
4. reserve one accepted turn and set the cooldown;
5. return a structured rejection reason when admission fails.

Defaults will be five turns per user per incident and twenty turns total per incident. The existing
`followup_max_turns` setting remains a deprecated alias for the per-user setting when the new
setting is not configured. General queries have no incident turn quota in this change, but use the
same scoped user cooldown.

An in-process lock was rejected because multiple bot replicas can share Redis. Incrementing only
after LLM success was rejected because concurrent requests could all pass the cap; an admitted
request consumes a turn even if downstream analysis later fails.

### 7. Migrate ephemeral Redis state without a blocking data migration

Retain the existing fingerprint turn key as the total incident counter and add a scoped per-user
counter. Introduce scoped cooldown keys. All keys keep the follow-up TTL, so legacy unscoped
cooldowns expire naturally and no bulk key rewrite is required.

On rollback, new keys are ignored by the old version and expire automatically. Existing registered
message-context records remain compatible because the change writes more records with the same
schema rather than changing their values.

## Risks / Trade-offs

- [Long answers can flood a channel] → Use conservative platform sizes, serialize batches, and
  measure chunk counts; a later change can add file/report overflow without weakening correctness.
- [Markdown edge cases can still be surprising] → Cover fences, lists, links, emphasis, tables,
  escaping, emoji, and oversized unbroken text with property-oriented and example-based tests;
  fall back to escaped plain text for indivisible constructs.
- [A batch can block urgent messages behind a rate-limit delay] → Bound retries and surface partial
  failure; keep the queue per channel rather than global.
- [Runtime Time configuration may be unavailable] → Validate `MaxPostSize` and use the conservative
  fallback.
- [New incident quota changes team behavior] → Default the per-user quota to the existing value,
  document the new total quota, and retain the legacy configuration alias.
- [An admitted request that fails downstream still consumes quota] → Define the quota as analysis
  attempts and expose operational metrics so repeated failures can be detected.

## Migration Plan

1. Add new settings with legacy alias handling and deploy-compatible Redis admission methods.
2. Add shared pagination, receipts, and delivery coordination without switching adapters.
3. Migrate adapters one at a time, with Discord first as the behavioral baseline, then Telegram,
   Slack, and Time.
4. Switch shared alert, command, rejection, and follow-up paths to consume delivery receipts and
   register all message IDs.
5. Enable SDK rate-limit support and add delivery/admission metrics and logs.
6. Remove the Discord-only continuation-ID workaround after parity tests pass.

Rollback can restore the previous adapter paths. New Redis keys and additional message-context
records are backward-compatible and expire through their existing TTLs.

## Open Questions

- Should a later capability replace large multi-message responses with a short chat summary plus a
  file or incident-report link after a configurable chunk threshold?
- Does the deployed Time service always expose `MaxPostSize` to bot accounts, or should operators
  also have an explicit `time_message_limit` override?
