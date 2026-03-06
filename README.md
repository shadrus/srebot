# AI Observability Bot 🤖

Telegram bot that monitors a Prometheus Alertmanager channel, analyzes each new alert using an LLM (any OpenAI-compatible API) with external **MCP (Model Context Protocol)** tools, and replies with a root-cause analysis in the same thread.

## How It Works

```
Alertmanager ──Telegram notification──► Channel
                                             │  bot polling
                                      Alert Parser
                                             │
                                   ┌─────── ▼ ────────┐
                                   │  LLM Agent loop   │
                                   └──┬───────────┬───┘
                                      │           │
                          (via Stdio) MCP Server 1   MCP Server 2
                               (e.g. Prometheus)   (e.g. SQLite/ES)
                                      │           │
                                   Bot replies in thread
```

## Quick Start

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, LLM_API_KEY
```

### 3. Configure Bot & MCP Servers

Create `config.yml` (starting from `config.yml.example`). This file defines LLM settings, ignore rules, and external tools.

```yaml
llm_model: "gpt-4o"
mcp_servers:
  prometheus:
    command: "uvx"
    args: ["prometheus-mcp-server", "--prometheus-url", "http://your-prometheus-url:9090"]
```

### 4. Run

```bash
# Docker (Recommended)
docker compose up -d

# Locally
uv run python -m ai_health_bot.bot.main
```

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHANNEL_ID` | Channel/group ID (negative) |
| `LLM_BASE_URL` | OpenAI-compat API base URL |
| `LLM_API_KEY` | API key |
| `LLM_MODEL` | Model name (e.g. `gpt-4o`) |
| `REDIS_URL` | Redis connection URL |
| `ALERT_FINGERPRINT_TTL` | Seconds to remember firing alert (default: 86400) |
| `MCP_SERVERS_CONFIG_PATH` | Path to `mcp_servers.yml` (default: `mcp_servers.yml`) |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |

## MCP Multi-Server Support

The bot is a pure MCP orchestrator. It doesn't have built-in tools. Instead, it connects to external MCP servers via Stdio (Standard Input/Output) on startup.

- **Dynamic Discovery**: The bot automatically downloads tools from each MCP server and provides them to the LLM.
- **Unified Registry**: All tools are merged into a single schema for the LLM.
- **Flexibility**: You can swap or add any MCP server without changing the bot's core code.

## Alert Filtering

You can ignore specific alerts based on their labels using flexible AND/OR logic in `ignore_rules.yml`.

- **Labels (AND)**: Matches if all specified labels match exactly.
- **Any (OR)**: Matches if at least one sub-condition matches.
- **All (AND)**: Matches if all sub-conditions match.

Example `ignore_rules.yml`:
```yaml
ignore_rules:
  - name: "Ignore Watchdog"
    condition:
      labels: { alertname: "Watchdog" }
  - name: "Ignore Prod-DB severity info"
    condition:
      all:
        - labels: { cluster: "prod", job: "database" }
        - labels: { severity: "info" }
```

## Deduplication

- First `Alerts Firing` for a fingerprint → analysis sent as thread reply
- Subsequent `Alerts Firing` with same fingerprint → silently ignored
- `Alerts Resolved` → fingerprint removed from Redis, "✅ Resolved" reply sent

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check src/ tests/
```

## Project Structure

```
src/ai_health_bot/
├── config.py          # Settings + MCPServerRegistry
├── parser/
│   └── alert_parser.py  # Telegram message parser
├── state/
│   └── store.py         # Redis dedup store
├── mcp/
│   ├── mcp_client.py    # Official MCP Protocol client (Stdio)
│   └── registry.py      # Dynamic tool registration and routing
├── llm/
│   ├── prompts.py       # System prompt + message builder
│   └── agent.py         # LLM tool-call loop
└── bot/
    ├── handlers.py      # channel_post_handler
    └── main.py          # Entry point (Lifecycle management)
```
