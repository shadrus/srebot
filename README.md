# SreBot 🤖

The **SREBot Agent** is a lightweight bridge that connects your private infrastructure (Prometheus, Loki, Elasticsearch) to the [SREBot AI Control Plane](https://srebot.site360.tech).

It listens to incident notifications in Telegram, Slack, Discord, or Time Messenger, securely polls your internal tools using the **Model Context Protocol (MCP)**, and replies with a detailed AI-generated root-cause analysis.

## How It Works

```text
Alertmanager ──chat notification──► Channel
                                             │
                                      SREBot Agent
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
             [SREBot Control Plane]                    [Your Private Infrastructure]
             (LLM & Analysis Logic)                    (Prometheus, Logs, etc.)
                       │                                           │
                       └───────────────────WebSocket───────────────┘
                                             │
                                   AI Analysis Reply
```

**Key Safety Feature:** Your infrastructure remains strictly internal. The Agent establishes an **outbound** WebSocket connection to the Control Plane. No incoming public access (Ingress) is required for your databases or logs.

---

## 🚀 Quick Start

### 1. Get Your Agent Token
1. Register at [srebot.site360.tech](https://srebot.site360.tech).
2. Go to **Settings** and copy your `SAAS_AGENT_TOKEN`.

### 2. Deployment (Docker Compose)
1. **Clone the repository:**
   ```bash
   git clone https://github.com/shadrus/srebot.git
   cd srebot
   ```
2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Set SAAS_AGENT_TOKEN and credentials for exactly one chat integration
   ```
3. **Run:**
   ```bash
   docker compose up -d
   ```

Only one chat integration may be configured in a bot process.

### Time Messenger

Time support uses the [`aiotimebot`](https://pypi.org/project/aiotimebot/) asyncio SDK and Time API v4. Create or obtain a bearer token for an account that belongs to the alert channel, then configure:

```dotenv
TIME_BASE_URL=https://time.example.com
TIME_TOKEN=replace-with-Time-bearer-token
TIME_CHANNEL_ID=replace-with-Time-channel-id
```

The integration receives posts over Time's authenticated WebSocket and uses REST for replies and edits. It has the same alert grouping, filtering, deduplication, automatic analysis, follow-up context, and `mute` / `unmute` / `status` command behavior as Telegram. Each analyzed alert group gets its own Time thread, so multi-alert notifications retain separate follow-up context; direct `@bot_username` mentions also start follow-up or general queries.

---

## ⚙️ Configuration

The Agent is configured via `config.yml`. It defines which **MCP Servers** the Agent should launch to talk to your tools.

### Example MCP Setup
```yaml
mcp_servers:
  prometheus:
    command: "uvx"
    args: ["prometheus-mcp-server"]
    env:
      PROMETHEUS_URL: "http://prometheus:9090"
```

The Agent will automatically:
1. Connect to the Prometheus MCP server.
2. Register its tools (querying, metrics, etc.).
3. Securely provide these tools to the SREBot AI when an incident occurs.

---

## 🛡 Security

- **Secrets Masking:** The Agent automatically redacts Bearer tokens and common passwords in tool outputs before they leave your network.
- **Read-Only Mode:** You can enforce `read_only: true` in `config.yml` for specific tools to ensure the AI cannot perform any mutating actions.
- **Zero Ingress:** Operates entirely within your private network via outbound communication.

## 📄 License
Released under the [PolyForm Noncommercial License 1.0.0](LICENSE).
