# AI Observability Bot 🤖

Telegram bot that monitors a Prometheus Alertmanager channel, analyzes each new alert using an LLM (any OpenAI-compatible API) with external **MCP (Model Context Protocol)** tools, and replies with a root-cause analysis in the same thread.

## How It Works

```text
Alertmanager ──Telegram notification──► Channel
                                             │  bot polling
                                      Alert Parser
                                             │
                                   ┌─────── ▼ ────────┐
                                   │  LLM Agent loop  │
                                   └──┬───────────┬───┘
                                      │           │
                          (via Stdio) MCP Server 1   MCP Server 2
                               (e.g. Prometheus)   (e.g. SQLite/ES)
                                      │           │
                                   Bot replies in thread
```

## Prerequisites

Before running the bot, you will need:
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Identifying the Telegram Channel ID where the bot will receive and reply to alerts (usually a negative number).
- An OpenAI-compatible API Key (e.g., OpenAI, Anthropic, DeepSeek).

---

## 🚀 Installation & Deployment

The bot can be launched in multiple environments depending on your infrastructure.

### Option A: Docker Compose (Recommended for VMs)

This is the fastest way to get the bot running along with a dedicated Redis instance for state deduplication.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/shadrus/ai-observability-bot.git
   cd ai-observability-bot
   ```

2. **Configure Environment Variables:**
   Copy the example environment file and fill in your credentials:
   ```bash
   cp .env.example .env
   ```
   *Make sure to set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, and `LLM_API_KEY`.*

3. **Configure MCP Servers & Bot Rules:**
   Copy the example config map:
   ```bash
   cp config.yml.example config.yml
   ```
   *Edit this file to define your tools (e.g., Prometheus MCP server), ignore rules, and LLM model.*

4. **Start the services:**
   ```bash
   docker compose up -d
   ```
   *This spins up the bot and a Redis container. The `config.yml` is mounted as read-only.*

### Option B: Kubernetes (Helm)

For production deployments targeting Kubernetes clusters, a Helm chart is provided in `deploy/helm/ai-observability-bot`.

1. **Navigate to the chart directory:**
   ```bash
   cd deploy/helm/ai-observability-bot
   ```

2. **Configure your `values.yaml`:**
   Modify the default `values.yaml` or create a new `custom-values.yaml`:
   - Set secret credentials under `secrets`:
     ```yaml
     secrets:
       telegram_bot_token: "YOUR_TOKEN"
       telegram_channel_id: "YOUR_CHANNEL_ID"
       llm_api_key: "YOUR_API_KEY"
     ```
   - Define MCP servers and ignore rules under `config`:
     ```yaml
     config:
       llm_model: "gpt-4o"
       mcp_servers:
         prometheus:
           command: "uvx"
           args:
             - "prometheus-mcp-server"
           env:
             PROMETHEUS_URL: "http://prometheus-operated.monitoring.svc:9090"
     ```

3. **Deploy to the cluster:**
   ```bash
   helm upgrade --install ai-observability-bot . -f custom-values.yaml --namespace monitoring --create-namespace
   ```
   *The chart supports connecting to an external Redis instance or provisioning a simple internal deployment (`redis.enabled: true`).*

### Option C: Local Development

For developing and testing the bot locally using `uv`.

1. **Install dependencies:**
   ```bash
   uv sync
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Fill in variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, LLM_API_KEY, REDIS_URL
   cp config.yml.example config.yml
   ```

3. **Run the bot:**
   ```bash
   uv run python -m ai_observability_bot.bot.main
   ```

---

## ⚙️ Configuration

### Environment Variables (.env)

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHANNEL_ID` | Channel/group ID (e.g. `-100123456789`) |
| `LLM_BASE_URL` | OpenAI-compat API base URL (Default: `https://api.openai.com/v1`) |
| `LLM_API_KEY` | API key for the LLM Provider |
| `LLM_MODEL` | Model name (e.g. `gpt-4o`) |
| `REDIS_URL` | Redis connection string (e.g. `redis://localhost:6379/0`) |
| `ALERT_FINGERPRINT_TTL` | Seconds to remember firing alert (default: 86400) |
| `CONFIG_PATH` | Path to `config.yml` (default: `config.yml`) |
| `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`) |

### MCP Multi-Server Support (`config.yml`)

The bot is a pure MCP orchestrator. It doesn't have built-in tools. Instead, it connects to external MCP servers via Stdio (Standard Input/Output) on startup.

- **Dynamic Discovery**: The bot automatically downloads tools from each MCP server and provides them to the LLM.
- **Dynamic Downloading**: Add any MCP server via `config.yml` using `npx -y <package>` or `uvx <package>`, and the bot will install it dynamically at runtime! No Docker rebuilds required.
- **Rule-based Routing**: Use `condition` blocks (same syntax as Alert Filtering) to restrict an MCP server so it's only available for specific alerts (e.g., use the Prod Database tools only for `cluster: prod` alerts).
- **Unified Registry**: All allowed tools are merged into a single schema for the LLM.

### Alert Filtering

You can ignore specific alerts using flexible AND/OR/NOT logic in `config.yml`:

```yaml
ignore_rules:
  - name: "Ignore Watchdog"
    condition:
      labels: { alertname: "Watchdog" }
  - name: "Ignore everything except prod"
    condition:
      not_labels: { cluster: "prod" }
  - name: "Ignore Prod-DB severity info"
    condition:
      all:
        - labels: { cluster: "prod", job: "database" }
        - labels: { severity: "info" }
```

### Deduplication

- **First `Alerts Firing`** for a fingerprint → Analysis is sent as a thread reply.
- **Subsequent `Alerts Firing`** with the same fingerprint → Silently ignored (configurable by `ALERT_FINGERPRINT_TTL`).
- **`Alerts Resolved`** → Fingerprint removed from Redis, "✅ Resolved" reply sent to close the thread logically.

---

## 🛠 Project Structure & Development

The project operates under strict rules defined in `AGENTS.md`.

```text
src/ai_observability_bot/
├── config.py          # Settings + MCPServerRegistry
├── parser/            # Telegram message parser & alert filtering logic
├── state/             # Redis dedup store
├── mcp/               # Official MCP Client (Stdio) & dynamic tool registry
├── llm/               # Prompts + LLM tool-call loop
└── bot/               # Telegram handlers & entry point (Lifecycle management)
```

**Testing & Formatting:**
```bash
# Run tests
uv run pytest tests/ -v

# Lint & Format
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```
