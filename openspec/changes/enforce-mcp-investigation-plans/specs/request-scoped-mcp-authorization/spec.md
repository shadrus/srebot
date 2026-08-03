## ADDED Requirements

### Requirement: Consistent incident MCP scope
The system SHALL derive the allowed MCP server set for every initial alert analysis and incident
follow-up from the validated primary alert and the configured server conditions.

#### Scenario: Follow-up retains matching cluster tools
- **WHEN** an engineer asks an incident follow-up and the saved primary alert matches a configured
  MCP server condition
- **THEN** the follow-up tool schema includes read-eligible tools from that server

#### Scenario: Follow-up excludes another cluster
- **WHEN** an engineer asks an incident follow-up and a registered MCP server condition does not
  match the saved primary alert
- **THEN** no tool from that server appears in the follow-up tool schema

#### Scenario: Routing configuration changes after initial analysis
- **WHEN** a new follow-up begins after MCP server conditions have changed
- **THEN** the system derives a new allowed-server set from the current configuration and saved
  primary alert

### Requirement: Fail-closed incident context routing
The system MUST use an empty incident tool schema when saved alert context is absent or cannot be
validated and MUST NOT interpret that condition as permission to use all registered tools.

#### Scenario: Malformed saved primary alert
- **WHEN** an incident follow-up resolves stored `alert_data` whose primary item is invalid
- **THEN** the follow-up continues with no MCP tools and diagnostics are reported as unavailable

#### Scenario: Empty saved alert list
- **WHEN** an incident follow-up has no saved primary alert
- **THEN** the incident path advertises no MCP tools rather than the global registry

### Requirement: Request-bound local tool authorization
The local agent MUST execute only exact prefixed tool names present in the schema advertised for the
active WebSocket analysis request.

#### Scenario: Advertised tool is requested
- **WHEN** the Control Plane requests a tool whose exact name is present in the active request schema
- **THEN** the local agent routes the call to its registered MCP client

#### Scenario: Registered but unadvertised tool is requested
- **WHEN** the Control Plane requests a globally registered tool that is absent from the active
  request schema
- **THEN** the local agent returns a structured tool error and does not invoke any MCP client

#### Scenario: Mixed authorized and unauthorized batch
- **WHEN** one execution batch contains both advertised and unadvertised tool names
- **THEN** advertised calls execute normally, unadvertised calls return errors, and every result
  remains associated with its original tool call ID

### Requirement: Stable in-flight authorization snapshot
The system SHALL keep the active tool-name allowlist fixed for the lifetime of one WebSocket
analysis loop.

#### Scenario: Registry gains a tool during analysis
- **WHEN** a tool becomes globally registered after an analysis request has advertised its schema
- **THEN** that tool remains unauthorized for the in-flight loop

#### Scenario: Subsequent analysis receives updated schema
- **WHEN** a later analysis advertises a schema containing the newly registered tool
- **THEN** that later loop can authorize the tool under its own snapshot

### Requirement: Explicit unscoped general queries
The system SHALL preserve the existing all-registered-tools catalog only for general queries that
have no incident context.

#### Scenario: General query without incident context
- **WHEN** a direct general diagnostic query starts without a resolved incident
- **THEN** the system may advertise all currently registered tools and binds execution to that
  advertised snapshot

#### Scenario: Incident follow-up omits caller allowlist
- **WHEN** a platform handler does not provide an allowed-server argument for an incident follow-up
- **THEN** shared incident routing derives a concrete scoped list and does not use general-query
  semantics
