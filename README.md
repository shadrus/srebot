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

### HTTP proxy

All four chat integrations support the process environment variable `HTTPS_PROXY`
for API requests and receiving events (Telegram polling or Slack/Discord/Time WebSocket).
For example, add this to `.env` when using Docker Compose:

```dotenv
HTTPS_PROXY=http://proxy.example.com:3128
```

For an authenticated proxy, use `http://user:password@proxy.example.com:3128`.
Use URL-safe credentials: the current Time WebSocket SDK does not decode
percent-encoded proxy usernames/passwords. The proxy must support HTTP CONNECT
to the chat service's HTTPS/WSS endpoints. For local runs, export `HTTPS_PROXY` in
the shell before starting the bot; reading `.env` into application settings alone
does not export variables to the SDKs. Unset the variable for direct connections.

These are process-wide proxy variables: other HTTP/WebSocket clients may also use
them. Discord selects one proxy using its API host (`discord.com`) and reuses it for
the gateway and attachments. Time REST selects by `TIME_BASE_URL`, including
`HTTP_PROXY` for an HTTP URL. Both selections respect `NO_PROXY`; Telegram and Time
WebSocket use their SDK's environment rules. Slack's SDK does not apply `NO_PROXY`.

### Time Messenger

Time support uses the [`aiotimebot`](https://pypi.org/project/aiotimebot/) asyncio SDK and Time API v4. Create or obtain a bearer token for an account that belongs to the alert channel, then configure:

```dotenv
TIME_BASE_URL=https://time.example.com
TIME_TOKEN=replace-with-Time-bearer-token
TIME_CHANNEL_ID=replace-with-Time-channel-id
```

The integration receives posts over Time's authenticated WebSocket and uses REST for replies and edits. It has the same alert grouping, filtering, deduplication, automatic analysis, follow-up context, and `mute` / `unmute` / `status` command behavior as Telegram. Each analyzed alert group gets its own Time thread, so multi-alert notifications retain separate follow-up context; direct `@bot_username` mentions also start follow-up or general queries.

### Long messages and follow-up limits

SREBot paginates long analyses, command results, follow-ups, cooldown notices, and quota notices
before delivery. Each chunk is rendered independently and sent in order. The conservative targets
are 3,800 visible characters for Telegram, 3,800 characters for Slack, and 1,900 characters for
Discord. Slack and Time continuations stay in the same thread, and replies to any delivered chunk
resolve to the same incident context. If delivery stops after some chunks, successfully delivered
chunks remain registered and the failed chunks are not replayed blindly.

Time reads `MaxPostSize` from `/api/v4/config/client` at startup and subtracts a 128-character
safety reserve. If the endpoint is unavailable or the value is invalid, it uses a 15,500-character
fallback.

Follow-up admission is scoped by platform, chat, and user and is reserved atomically in Redis:

```dotenv
# Deprecated per-user alias; retained for backward compatibility.
FOLLOWUP_MAX_TURNS=5
# Optional per-user, per-incident override. Leave empty or unset to use FOLLOWUP_MAX_TURNS.
FOLLOWUP_USER_MAX_TURNS=
# Shared cap across all users for one incident.
FOLLOWUP_INCIDENT_MAX_TURNS=20
FOLLOWUP_USER_COOLDOWN_SEC=10
FOLLOWUP_TTL=43200
```

Accepted incident follow-ups consume both the user's incident quota and the total incident quota.
General queries use the scoped cooldown but do not consume incident turns. Empty values in the
environment template are ignored, so unused optional and integer placeholders retain their
application defaults.

---

## ⚙️ Configuration

The Agent is configured via `config.yml`. It defines which **MCP Servers** the Agent should launch to talk to your tools.

### Example MCP Setup
```yaml
mcp_servers:
  prometheus:
    url: "http://localhost:18000/sse"
    transport: "sse"
    pool_size: 4
    read_only: true
```

The Agent will automatically:
1. Open the configured number of independent connections to the Prometheus MCP server.
2. Register its tools (querying, metrics, etc.).
3. Securely provide these tools to the SREBot AI when an incident occurs.

`pool_size` defaults to `1` and accepts values from `1` to `32`. Startup fails unless every
configured connection is ready. After startup, temporary MCP outages fail individual tool calls;
later calls reconnect lazily without restarting the bot.

---

## 🛡 Security

- **Secrets Masking:** The Agent automatically redacts Bearer tokens and common passwords in tool outputs before they leave your network.
- **Read-Only Mode:** You can enforce `read_only: true` in `config.yml` for specific tools to ensure the AI cannot perform any mutating actions.
- **Zero Ingress:** Operates entirely within your private network via outbound communication.

## 📄 License
Released under the [PolyForm Noncommercial License 1.0.0](LICENSE).
