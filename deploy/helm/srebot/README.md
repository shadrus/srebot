# SREBot Helm Chart

[SREBot](https://srebot.site360.tech) is an AI-powered incident analyzer for Prometheus and Alertmanager, integrated directly into Telegram.

## Pre-requisites

- Kubernetes 1.19+
- Helm 3.0+

## Installation

Add the Helm repository and install the chart:

```bash
helm repo add srebot https://shadrus.github.io/srebot
helm install srebot srebot/srebot --namespace devops --create-namespace
```

## Configuration Examples

SREBot is highly customizable via `values.yaml`. Below are some common configuration scenarios:

### 1. Using an Existing Kubernetes Secret (Recommended for GitOps/Production)

Instead of passing raw tokens in values, create a Kubernetes Secret in your namespace and instruct the chart to use it:

```yaml
secrets:
  # Reference the name of your pre-created Kubernetes Secret
  existingSecret: "srebot-secret"
```

Your `srebot-secret` must contain the following keys:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`
- `SAAS_AGENT_TOKEN`

### 2. Passing Tokens Directly

For quick local testing, you can pass the strings directly (the chart will generate a Secret for you):

```yaml
secrets:
  telegram_bot_token: "telegram:token"
  telegram_channel_id: "telegram:channel_id"
  saas_agent_token: "saas:token"
```

### 3. Configuring MCP Servers (Sidecars)

As of version 0.1.0, SREBot connects to MCP servers via network (SSE/HTTP). The most reliable way in Kubernetes is to run them as **sidecar containers** within the same Pod.

> [!WARNING]
> **Root Level Only:** The `sidecars` section must be at the **root** of your `values.yaml` (next to `config:`). **DO NOT** nest it inside the `config:` section.
> **Kubernetes Syntax:** Kubernetes uses a **list** for environment variables (`- name: ... / value: ...`). Do not use the dictionary syntax from Docker Compose.

#### Step A: Define the sidecar in `values.yaml`
```yaml
# Correct structure
config:
  agentToken: "..."

# sidecars is NOT inside config
sidecars:
  prometheus-mcp:
    image: ghcr.io/pab1it0/prometheus-mcp-server:latest
    env:
      - name: PROMETHEUS_URL
        value: "http://prometheus-operated.monitoring.svc:9090"
      - name: PROMETHEUS_MCP_SERVER_TRANSPORT
        value: "sse"
    ports:
      - containerPort: 8080

  elasticsearch-mcp:
    image: docker.elastic.co/mcp/elasticsearch:latest
    # In Kubernetes, use 'args' to append to the image's ENTRYPOINT
    args: ["http", "--address", "0.0.0.0:18001"]
    env:
      - name: ES_URL
        value: "http://elasticsearch-master:9200"
    ports:
      - containerPort: 18001
```

#### Step B: Point SREBot to the sidecar
All containers in a Pod share the same network namespace, so use `localhost`:
```yaml
config:
  mcp_servers:
    prometheus:
      url: "http://localhost:8080/sse"
      transport: "sse"
```

### 4. Ignoring Specific Alerts

You can prevent SREBot from analyzing and firing notifications for noisy standard alerts or specific environments. The `ignore_rules` support `labels`, `not_labels`, `any`, and `all` logic.

```yaml
config:
  ignore_rules:
    # Example 1: Ignore by exact label match
    - name: "Ignore Watchdog"
      condition:
        labels: 
          alertname: "Watchdog"

    # Example 2: Ignore EVERYTHING except the production cluster
    # (i.e. if cluster is NOT "prod", it will be ignored)
    - name: "Ignore Non-Prod"
      condition:
        not_labels:
          cluster: "prod"

    # Example 3: Ignore if ANY of the conditions are met (OR logic)
    - name: "Ignore Dev or Low Severity"
      condition:
        any:
          - labels: { cluster: "dev" }
          - labels: { severity: "info" }

    # Example 4: Ignore only if ALL conditions are met (AND logic)
    - name: "Ignore CPU warnings in specific namespace"
      condition:
        all:
          - labels: { alertname: "HighCPUUsage" }
          - labels: { namespace: "kube-system" }
```

### 5. Using External Redis

By default, the chart deploys a simple internal Redis pod. For production, it's recommended to connect to an external highly-available Redis:

```yaml
redis:
  enabled: false
  url: "redis://my-redis-cluster.database.svc.cluster.local:6379/1"
```


## License

This software is distributed under the PolyForm Noncommercial 1.0.0 license.
