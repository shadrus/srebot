# Cross-Platform Message Delivery

## Purpose

Define safe, complete, and ordered delivery of platform-sized bot responses.

## Requirements

### Requirement: Platform-aware message constraints
The system SHALL apply a platform-specific conservative message-size target to every user-visible
text response before sending or editing it.

#### Scenario: Known platform target
- **WHEN** the bot prepares a Telegram, Slack, or Discord response
- **THEN** it uses that platform's configured conservative target rather than a universal limit

#### Scenario: Time target discovered at runtime
- **WHEN** the Time client configuration provides a valid `MaxPostSize`
- **THEN** the bot derives its Time target from that value with a safety reserve

#### Scenario: Time target unavailable
- **WHEN** the Time client configuration does not provide a valid `MaxPostSize`
- **THEN** the bot uses the documented conservative fallback target

### Requirement: Content-preserving semantic pagination
The system MUST split an oversized canonical Markdown response into independently valid formatted
chunks without dropping visible source content.

#### Scenario: Response fits one message
- **WHEN** the rendered response is within the platform target
- **THEN** the system produces exactly one chunk with the complete response

#### Scenario: Response crosses block boundaries
- **WHEN** the response exceeds the platform target and contains suitable Markdown block boundaries
- **THEN** the system splits at those boundaries before considering line, whitespace, or grapheme
  boundaries

#### Scenario: Oversized fenced code block
- **WHEN** one fenced code block cannot fit in a chunk
- **THEN** each resulting chunk has a valid closing and reopening fence with the original language
  marker and contains all code content in order

#### Scenario: Indivisible formatted construct
- **WHEN** an inline construct cannot be divided without invalid markup
- **THEN** the system emits that construct as safely escaped plain text across valid chunks

#### Scenario: Oversized unbroken text
- **WHEN** text contains no earlier safe split point
- **THEN** the system splits on Unicode grapheme boundaries without exceeding the platform target

### Requirement: Independently valid platform rendering
The system SHALL render each logical page separately for the destination platform so no chunk
contains unbalanced or unsupported platform markup.

#### Scenario: Telegram HTML pagination
- **WHEN** a paginated response is sent to Telegram
- **THEN** every chunk is independently valid Telegram HTML and stays within the target measured
  after entity parsing

#### Scenario: Markdown-family pagination
- **WHEN** a paginated response is sent to Slack, Discord, or Time
- **THEN** every chunk uses the destination's supported Markdown dialect and remains independently
  renderable

### Requirement: Ordered batch delivery
The system MUST deliver all chunks of one response in order without interleaving chunks from
another response in the same platform channel.

#### Scenario: Concurrent analyses in one channel
- **WHEN** two alert analyses complete concurrently for the same channel
- **THEN** the coordinator completes or exhausts one delivery batch before sending chunks from the
  other batch

#### Scenario: Existing placeholder
- **WHEN** a response replaces an analyzing placeholder
- **THEN** the system writes the first chunk to that placeholder when possible and sends remaining
  chunks as ordered continuations

#### Scenario: Placeholder edit fails
- **WHEN** the first-chunk placeholder edit cannot be completed
- **THEN** the system sends a new first message and continues the batch from that message without
  discarding chunks

### Requirement: Complete delivery receipts
Every multi-message delivery attempt SHALL return a receipt containing its primary message ID, all
successfully delivered message IDs, expected and delivered chunk counts, and terminal delivery
status.

#### Scenario: Complete delivery
- **WHEN** every chunk is delivered
- **THEN** the receipt status is `complete` and contains an ID for every chunk

#### Scenario: Partial delivery
- **WHEN** at least one chunk succeeds and a later chunk exhausts retry handling
- **THEN** the receipt status is `partial` and contains only the successfully delivered IDs

#### Scenario: Failed delivery
- **WHEN** no chunk is delivered
- **THEN** the receipt status is `failed` and contains no primary message ID

### Requirement: Reply context for every chunk
The system MUST associate every successfully delivered analysis or follow-up chunk with the same
fingerprint and incident context.

#### Scenario: Reply to continuation chunk
- **WHEN** a user replies to any continuation chunk
- **THEN** the bot resolves the same follow-up context as a reply to the primary chunk

#### Scenario: Partial delivery context
- **WHEN** a delivery is partial
- **THEN** every successfully delivered chunk is registered even though later chunks failed

### Requirement: Safe platform rate-limit handling
The system SHALL respect platform-provided retry delays and SHALL bound retries without blindly
replaying sends whose result is ambiguous.

#### Scenario: Explicit rate-limit response
- **WHEN** a platform rejects an operation with a retry delay
- **THEN** the operation waits for the provided delay and retries only within the configured bound

#### Scenario: Retry budget exhausted
- **WHEN** a rate-limited operation exhausts its retry budget
- **THEN** delivery stops with a partial or failed receipt and does not replay completed chunks

#### Scenario: Ambiguous non-idempotent failure
- **WHEN** a non-idempotent send fails without proving that the platform rejected it
- **THEN** the application does not add a blind generic retry that could duplicate the message

#### Scenario: Independent channels
- **WHEN** one channel is delayed by rate limiting
- **THEN** delivery to a different channel is not blocked by the first channel's queue
