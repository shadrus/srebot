# Contextual Skill Selection

## Purpose

Define scope-aware, token-efficient selection and loading of user skills for LLM analyses.

## Requirements

### Requirement: Scope-filtered candidate catalog
The system SHALL build the selector catalog only from active skills visible to the authenticated
organization and agent token. Global and organization skills SHALL follow their existing scope
rules, and bot-scoped skills SHALL be candidates only when assigned to the current agent token.
The system SHALL apply existing trigger rules before adding a visible skill to the catalog.

#### Scenario: Unassigned bot skill is excluded
- **WHEN** an analysis starts for an agent token that is not assigned a bot-scoped skill
- **THEN** the skill is absent from the selector catalog

#### Scenario: No eligible skills
- **WHEN** no active skills are visible to the organization and agent token
- **THEN** the system skips skill selection and starts the main analysis without a skill block

#### Scenario: Existing trigger does not match
- **WHEN** a visible skill has trigger rules that do not match the alert or question
- **THEN** the skill is absent from the selector catalog

### Requirement: Compact automatic selection
Before the main alert, follow-up, or general-analysis completion, the system SHALL ask an LLM
selector to choose only directly relevant skills from the candidate catalog. The selector request
MUST contain skill IDs, names, descriptions, and scope, request context, and compact available-tool
metadata, and MUST NOT contain full skill content.

#### Scenario: GitLab skill is irrelevant to a metric alert
- **WHEN** the catalog contains a GitLab project-analysis skill and the request concerns only a CPU
  metric alert
- **THEN** the selector can return an empty list or other directly relevant skills without loading
  the GitLab skill content

#### Scenario: Request matches a project-analysis skill
- **WHEN** the request explicitly asks to investigate a GitLab pipeline or job and the catalog
  contains a matching skill description
- **THEN** the selector may return that skill ID

### Requirement: Zero-to-three selection boundary
The system SHALL accept zero selected skills and MUST activate no more than three skills for one
analysis. Three is a maximum and the selector MUST NOT fill unused positions with marginally
relevant skills.

#### Scenario: No relevant skill
- **WHEN** none of the candidate descriptions is directly relevant
- **THEN** the selector returns no selected skill IDs and the main analysis proceeds normally

#### Scenario: Selector returns more than three IDs
- **WHEN** a selector response contains more than three valid candidate IDs
- **THEN** the system activates only the first three unique valid IDs

### Requirement: Validated fail-closed selection
The system MUST intersect selected IDs with the eligible candidate set. If selector execution
fails or its response cannot be parsed, the system SHALL continue the main analysis without skills.

#### Scenario: Unknown skill ID
- **WHEN** the selector returns an ID that is not in the eligible candidate catalog
- **THEN** the system ignores that ID

#### Scenario: Malformed selector response
- **WHEN** the selector response is not valid supported JSON
- **THEN** the system activates no skills and still performs the requested analysis

#### Scenario: Selector response is truncated by its completion budget
- **WHEN** the selector reports a length-limited response that cannot be parsed
- **THEN** the system retries once with a larger configured completion budget
- **AND** activates no skills if the retry also fails

### Requirement: Full content only for selected skills
The system SHALL append the complete content of each selected skill to the main system prompt and
SHALL omit every unselected skill's content. Selected skills SHALL be rendered in deterministic
global, organization, then bot scope order.

#### Scenario: One of multiple candidates selected
- **WHEN** only one of several candidate skills is selected
- **THEN** the main system prompt contains the selected skill's full content and none of the other
  candidates' content

### Requirement: Selector token accounting
The system SHALL add selector prompt and completion usage to the current incident token counters.

#### Scenario: Selector reports usage
- **WHEN** the selector completion returns token usage
- **THEN** those prompt and completion tokens are included in incident billing totals

#### Scenario: Truncated selector response is retried
- **WHEN** the selector performs a second completion after a length-limited response
- **THEN** usage from both completions is included in incident billing totals

### Requirement: Simple selection description
The system SHALL reuse the existing description field as the text that explains when the skill
should be used and SHALL NOT add new routing fields for modes, capabilities, or MCP tool patterns.

#### Scenario: Existing skill remains routable
- **WHEN** an existing skill already has a non-empty description
- **THEN** that description is preserved and used in the selector catalog without a new database
  field

#### Scenario: New blank description
- **WHEN** a user attempts to create a skill with a blank description
- **THEN** validation rejects the request and asks for a non-empty “When to use” value

### Requirement: Trigger-rule compatibility
The system SHALL preserve existing trigger-rule persistence, API contracts, editing, display, and
matching behavior. Trigger matching SHALL act as a deterministic pre-filter before LLM selection.

#### Scenario: User edits a skill
- **WHEN** a user opens or saves a skill in the dashboard
- **THEN** existing trigger-rule controls and payload behavior remain available

#### Scenario: Skill has no trigger rules
- **WHEN** a visible active skill has no trigger rules
- **THEN** it remains a selector candidate but its full content is loaded only if selected
