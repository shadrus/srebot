# SRE Bot

An observability assistant that investigates chat requests and reports operational findings.

## Language

**External MCP server**:
A configured integration endpoint that exposes private observability tools to the bot through MCP under one server identity.
_Avoid_: MCP client, data source

**Progress message**:
A temporary chat message that communicates the bot's current user-visible phase or accepted action while a request is being processed.
_Avoid_: Thinking message, reasoning message, activity log

**Public status**:
A short model-authored description of an accepted next action and its immediate purpose that is safe to show in a progress message. It describes intent without exposing hypotheses or the model's reasoning.
_Avoid_: Chain of thought, tool trace, reasoning summary

**Analysis**:
A chat request that runs an LLM investigation — an alert analysis, a general query, or a follow-up analysis.
_Avoid_: job, task

**Concurrency slot**:
A permit an Analysis holds while its LLM investigation runs; concurrency limits count simultaneous slots, not totals.
_Avoid_: rate limit, cooldown

**Turn quota**:
The total number of follow-up turns admitted for one user or one incident within the follow-up context window.
_Avoid_: rate limit, concurrency limit

**Root incident**:
The incident created by the initial analysis of an alert group.
_Avoid_: base incident

**Incident branch**:
The chain of incidents produced by follow-up replies; each response records its own incident ID so later replies continue that branch.
_Avoid_: thread, conversation

**Task supervisor**:
The Time Messenger component that owns background Analyses from event arrival through completion and shutdown.
_Avoid_: worker pool, task queue
