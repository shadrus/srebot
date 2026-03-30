# SREBot Agent 🤖

The **SREBot Agent** is a lightweight bridge that connects your private infrastructure (Prometheus, Loki, Elasticsearch) to the [SREBot AI Control Plane](https://srebot.site360.tech).

It listens to incident notifications in a Telegram channel, securely polls your internal tools using the **Model Context Protocol (MCP)**, and replies with a detailed AI-generated root-cause analysis.

## How It Works

```text
Alertmanager ──Telegram notification──► Channel
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
   git clone https://github.com/shadrus/ai-observability-bot.git
   cd ai-observability-bot
   ```
2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Set TELEGRAM_BOT_TOKEN and SAAS_AGENT_TOKEN
   ```
3. **Run:**
   ```bash
   docker compose up -d
   ```

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
