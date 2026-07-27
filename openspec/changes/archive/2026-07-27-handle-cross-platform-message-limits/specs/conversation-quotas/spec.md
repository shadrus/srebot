## ADDED Requirements

### Requirement: Scoped conversation identity
The system MUST scope user cooldown and per-user quota state by platform, chat, and platform user
identifier.

#### Scenario: Same raw user ID on different platforms
- **WHEN** Telegram and Discord users have the same raw identifier
- **THEN** their cooldown and quota state remain independent

#### Scenario: Same user in different chats
- **WHEN** one platform user interacts with the bot in two configured chat identities over time
- **THEN** each chat has independent cooldown state

### Requirement: Context validation before quota admission
The system SHALL validate that an incident follow-up has usable context before consuming cooldown
or turn quota.

#### Scenario: Reply has no bot context
- **WHEN** a reply does not resolve to a bot message or active incident
- **THEN** the request is rejected without setting cooldown or incrementing a turn counter

#### Scenario: Incident context expired
- **WHEN** a reply references an incident whose follow-up context has expired
- **THEN** the request is rejected without consuming an incident turn

### Requirement: Atomic follow-up admission
The system MUST atomically evaluate and reserve cooldown, per-user incident turns, and total
incident turns for a valid follow-up.

#### Scenario: Accepted incident follow-up
- **WHEN** the user is outside cooldown and both turn quotas have capacity
- **THEN** one user turn and one total incident turn are reserved and the user cooldown is set as
  one atomic admission

#### Scenario: Concurrent final slots
- **WHEN** concurrent requests compete for the final available quota slot
- **THEN** no more requests are admitted than the configured limit

#### Scenario: Admitted analysis later fails
- **WHEN** an admitted request reaches analysis but downstream processing fails
- **THEN** its reserved turn remains consumed as an analysis attempt

### Requirement: Per-user incident quota
The system SHALL enforce a configurable maximum number of admitted follow-up turns for each scoped
user within one incident.

#### Scenario: One user reaches their limit
- **WHEN** a user has consumed their configured incident turns
- **THEN** further follow-ups from that user are rejected with the localized limit response while
  other users retain their own quota

#### Scenario: Legacy configuration
- **WHEN** the new per-user setting is absent and the legacy `followup_max_turns` setting is present
- **THEN** the system uses the legacy value as the per-user incident limit

### Requirement: Total incident quota
The system SHALL enforce a configurable maximum number of admitted follow-up turns across all users
for one incident.

#### Scenario: Team reaches incident limit
- **WHEN** the total number of admitted turns reaches the incident limit
- **THEN** further incident follow-ups from every user are rejected with a localized incident-limit
  response

#### Scenario: Independent incidents
- **WHEN** one incident reaches its total limit
- **THEN** follow-ups for another incident remain unaffected

### Requirement: Scoped cooldown
The system SHALL reject rapid repeated requests from the same scoped user for the configured
cooldown duration without consuming an additional incident turn.

#### Scenario: Request during cooldown
- **WHEN** an otherwise valid follow-up arrives before the scoped user's cooldown expires
- **THEN** the request receives the localized cooldown response and neither turn counter changes

#### Scenario: Request after cooldown
- **WHEN** an otherwise valid follow-up arrives after the cooldown expires and quotas have capacity
- **THEN** the request can be admitted

#### Scenario: General query cooldown
- **WHEN** a direct mention starts a general query without incident context
- **THEN** the scoped user cooldown applies but incident turn quotas do not

### Requirement: Expiring quota state
The system MUST expire cooldown and incident quota keys consistently with their configured windows.

#### Scenario: Incident follow-up TTL expires
- **WHEN** an incident's follow-up TTL expires
- **THEN** its per-user and total turn keys expire without a manual migration

#### Scenario: Legacy unscoped cooldown
- **WHEN** a deployment introduces scoped cooldown keys
- **THEN** legacy unscoped cooldown keys are left to expire and do not block the scoped identity
