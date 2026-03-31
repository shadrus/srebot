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

### 3. Configuring MCP Servers (e.g. Prometheus)

You can configure external Model Context Protocol (MCP) servers so SREBot can fetch live metrics during an incident. For example, connecting to Prometheus:

```yaml
config:
  mcp_servers:
    prometheus:
      command: "uvx"
      args:
        - "prometheus-mcp-server"
      env:
        # Update this URL to point to your internal Prometheus service
        PROMETHEUS_URL: "http://prometheus-operated.monitoring.svc:9090"
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
