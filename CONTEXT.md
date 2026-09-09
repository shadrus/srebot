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
