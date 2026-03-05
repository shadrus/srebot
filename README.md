# AI Health Bot 🤖

Telegram bot that monitors a Prometheus Alertmanager channel, analyzes each new alert
using an LLM (any OpenAI-compatible API) with MCP tools (Prometheus, Elasticsearch),
and replies with a root-cause analysis in the same thread.

Duplicate alerts (same fingerprint, still firing) are silently ignored until resolved.
Multiple Kubernetes clusters are supported out of the box.

## How It Works

```
Alertmanager ──Telegram notification──► Channel
                                            │  bot polling
                                     Alert Parser
                                            │
                                  ┌─────── ▼ ────────┐
                                  │  LLM Agent loop   │
                                  └──┬───────────┬───┘
                                     │           │ (by cluster label)
                               Prometheus[c]   ES[c]
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
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
```

### 3. Configure clusters

Edit `clusters.yml` — add one entry per Kubernetes cluster.
The key **must match** the `cluster` label in your Prometheus alerts.

```yaml
clusters:
  google-production:
    prometheus_url: http://prometheus:9090
    elasticsearch_url: http://es:9200
    elasticsearch_index_pattern: "logs-*"
```

### 4. Add bot to channel

1. Create a bot via [@BotFather](https://t.me/BotFather) → get token
2. Add the bot to your Alertmanager Telegram channel **as admin**
3. Set `TELEGRAM_CHANNEL_ID` to the channel ID (negative number for channels/groups)

### 5. Run

```bash
# Locally
uv run python -m ai_health_bot.bot.main

# Docker
docker compose up -d
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
| `CLUSTERS_CONFIG_PATH` | Path to `clusters.yml` (default: `clusters.yml`) |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |

## Multi-Cluster Support

The bot reads the `cluster` label from each alert and routes all tool calls
(Prometheus queries, Elasticsearch searches) to the correct cluster endpoints
defined in `clusters.yml`.

No limit on the number of clusters — add as many as needed.

## Deduplication

- First `Alerts Firing` for a fingerprint → analysis sent as thread reply
- Subsequent `Alerts Firing` with same fingerprint → silently ignored
- `Alerts Resolved` → fingerprint removed from Redis, "✅ Resolved" reply sent
- Next firing after resolution → fresh analysis triggered

## Available LLM Tools

| Tool | Description |
|---|---|
| `query_prometheus` | Instant PromQL query |
| `query_prometheus_range` | Range PromQL query (metric history) |
| `get_active_alerts` | List currently firing alerts |
| `search_logs` | Full-text log search in Elasticsearch |
| `get_index_stats` | Elasticsearch index statistics |

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
├── config.py          # Settings + ClusterRegistry
├── parser/
│   └── alert_parser.py  # Telegram message parser
├── state/
│   └── store.py         # Redis dedup store
├── mcp/
│   ├── tools.py         # Prometheus + ES tool functions
│   └── registry.py      # OpenAI function-calling schema builder
├── llm/
│   ├── prompts.py       # System prompt + message builder
│   └── agent.py         # LLM tool-call loop
└── bot/
    ├── handlers.py      # channel_post_handler
    └── main.py          # Entry point
```
