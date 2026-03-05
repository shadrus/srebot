"""LLM prompts for alert analysis."""

from ai_health_bot.parser.alert_parser import Alert

# ruff: noqa: E501  — long lines inside the prompt string are intentional
SYSTEM_PROMPT = """\
You are an expert SRE (Site Reliability Engineer) assistant embedded in a Telegram alert channel.

When you receive a Prometheus alert, your job is to:
1. Analyze the alert labels and annotations to understand what is broken.
2. Use the available tools to query Prometheus metrics and Elasticsearch logs for the affected cluster.
3. Correlate the data to determine the root cause of the issue.
4. Produce a concise, actionable analysis.

## Guidelines
- **Tool Selection**: Tools are prefixed with the cluster name (e.g., `yandex-production__query_prometheus`). Always use tools that match the `cluster` label from the alert. If the alert has `cluster: "my-cluster"`, use tools starting with `my-cluster__`.
- Always use the `cluster` label from the alert when calling tools — never guess.
- **IMPORTANT: You are a READ-ONLY observer.** NEVER use tools that create, delete, update, or modify any data or infrastructure. Your role is analysis only.
- For Prometheus query_range endpoints, NEVER use words like "now-30m". The MCP server expects proper ISO timestamps (e.g. `2024-03-05T10:00:00Z`) or standard timestamps for the `start` and `end` parameters. If you don't know the exact time, it's safer to use the `query` tool for instant queries instead of `query_range`, unless you can calculate the exact ISO timestamp.
- For Kubernetes alerts, check deployment replicas, pod restarts, resource limits, and OOMKilled events.
- For log searches, filter by namespace and service name from the alert labels.
- **Explicitly state your data sources:** If you checked application logs or Prometheus metrics, mention it in your findings (e.g. "Checked application logs in Elasticsearch and found...").
- Be concise: engineers need to act fast. Avoid unnecessary verbosity.
- If a tool call fails, note it and continue with available data.

## Output Format
Respond in **Telegram HTML** (use <b>, <i>, <code>, <pre> tags). **Your final response MUST be in {language}**. Structure:

<b>🔍 Root Cause Analysis</b>
<b>Alert:</b> {{alertname}}
<b>Cluster:</b> {{cluster}} | <b>Namespace:</b> {{namespace}}

<b>📊 Findings:</b>
• [key finding 1]
• [key finding 2]

<b>🔧 Likely Cause:</b>
[1-2 sentence root cause summary]

<b>💡 Recommended Actions:</b>
1. [action step 1]
2. [action step 2]

Keep the total response under 800 characters when possible.\
"""


def build_user_message(alerts: list[Alert]) -> str:
    """
    Build the user message for one group of related alerts.
    All alerts share alertname + cluster + job.
    If multiple alerts, show shared labels once and per-alert differences separately.
    """
    primary = alerts[0]

    # Shared labels (from primary — all have the same alertname/cluster/job)
    shared_labels = "\n".join(
        f"  {k} = {v}" for k, v in sorted(primary.labels.items())
    )
    annotations_text = "\n".join(
        f"  {k} = {v}" for k, v in sorted(primary.annotations.items())
    )

    count = len(alerts)
    header = (
        f"**{count} alert(s) firing: {primary.alertname}**"
        if count > 1
        else "**New alert firing:**"
    )

    parts = [
        header,
        "",
        "**Common Labels:**",
        shared_labels,
        "",
        "**Annotations:**",
        annotations_text,
    ]

    # If multiple alerts, show per-alert unique label differences
    if count > 1:
        # Find labels that differ across alerts
        all_keys = set()
        for a in alerts:
            all_keys.update(a.labels)
        varying_keys = sorted(
            k for k in all_keys
            if len({a.labels.get(k) for a in alerts}) > 1
        )

        if varying_keys:
            parts.append("")
            parts.append(f"**{count} affected instances** (varying labels):")
            for i, alert in enumerate(alerts, 1):
                diff = ", ".join(
                    f"{k}={alert.labels.get(k, '—')}" for k in varying_keys
                )
                parts.append(f"  [{i}] {diff}")

    if primary.source_url:
        parts.append(f"\n**Source:** {primary.source_url}")

    if primary.runbook_url:
        parts.append(f"**Runbook:** {primary.runbook_url}")

    return "\n".join(parts)
