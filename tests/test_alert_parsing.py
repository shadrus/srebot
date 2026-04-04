from srebot.parser.alert_parser import AlertStatus, parse_alert_message, update_remote_strategies


def setup_module(module):
    """Seed the dynamis parser with same strategies used in prod for local tests."""
    update_remote_strategies(
        [
            {
                "name": "Standard",
                "firing_pattern": r"(alerts?\s+firing|\[FIRING:.*\]|FIRING|🔥|\*Alert:\*)",
                "resolved_pattern": r"(alerts?\s+resolved|\[RESOLVED:.*\]|RESOLVED|✅|\[RESOLVED\])",
                "labels_header_pattern": r"^(Labels|Details):\s*$",
                "annotations_header_pattern": r"^Annotations:\s*$",
                "kv_pattern": r"^\s*[\-•]\s*(.+?)\s*[=:]\s*(.+)$",
                "priority": 10,
            },
            {
                "name": "Markdown",
                "firing_pattern": r"(alerts?\s+firing|\[FIRING:.*\]|FIRING|🔥|\*Alert:\*)",
                "resolved_pattern": r"(alerts?\s+resolved|\[RESOLVED:.*\]|RESOLVED|✅|\[RESOLVED\])",
                "labels_header_pattern": r"^\*?(Details|Labels):\*?\s*$",
                "annotations_header_pattern": r"^\*?Annotations:\*?\s*$",
                "kv_pattern": r"^\s*\*?\s*[•\-]?\s*\*?\s*(.+?)\s*\*?\s*[:=]\*?\s*(.+)$",
                "priority": 20,
            },
        ]
    )


STANDARD_ALERT = """
🚨 Alerts Firing:

Labels:
 - alertname = CPUUsageHigh
 - cluster = prod-1
 - job = node-exporter
 - severity = critical
Annotations:
 - summary = CPU usage is above 90%
Source: http://prometheus/graph
"""

CUSTOM_ALERT = """
🔥 [FIRING:1] KubePodNotReady
Alert: Pod has been in a non-ready state for more than 15 minutes. - warning

Details:
 • alertname: KubePodNotReady
 • alertgroup: kubernetes-apps
 • cluster: management-cluster
 • namespace: canton
 • pod: validator-app-7d77cc9cb-w7wlc
 • severity: warning

Annotations:
 • summary: Pod is not ready
Source: http://alertmanager/details
"""

MARKDOWN_BOLD_ALERT = """
*Alert:* Pod has been in a non-ready state for more than 15 minutes. - `warning`
*Description:* Pod canton/validator-app-7d77cc9cb-w7wlc has been in a non-ready state \
for longer than 15 minutes on cluster management-cluster.
*Details:*
 • *alertname:* `KubePodNotReady`
 • *alertgroup:* `kubernetes-apps`
 • *cluster:* `management-cluster`
 • *namespace:* `canton`
 • *pod:* `validator-app-7d77cc9cb-w7wlc`
 • *severity:* `warning`

*Annotations:*
 • *summary:* Pod is not ready
Source: http://alertmanager/details
"""


def test_parse_standard_alert():
    alerts = parse_alert_message(STANDARD_ALERT)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.status == AlertStatus.FIRING
    assert a.alertname == "CPUUsageHigh"
    assert a.cluster == "prod-1"
    assert a.labels["job"] == "node-exporter"
    assert a.source_url == "http://prometheus/graph"


def test_parse_custom_alert():
    alerts = parse_alert_message(CUSTOM_ALERT)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.status == AlertStatus.FIRING
    assert a.alertname == "KubePodNotReady"
    assert a.cluster == "management-cluster"
    assert a.labels["namespace"] == "canton"
    assert a.labels["pod"] == "validator-app-7d77cc9cb-w7wlc"
    assert a.source_url == "http://alertmanager/details"


def test_parse_markdown_bold_alert():
    alerts = parse_alert_message(MARKDOWN_BOLD_ALERT)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.status == AlertStatus.FIRING
    assert a.alertname == "KubePodNotReady"
    assert a.cluster == "management-cluster"
    assert a.labels["namespace"] == "canton"
    assert a.labels["pod"] == "validator-app-7d77cc9cb-w7wlc"
    assert a.source_url == "http://alertmanager/details"


def test_parse_resolved_custom_alert():
    resolved_text = CUSTOM_ALERT.replace("[FIRING:1]", "[RESOLVED:1]")
    alerts = parse_alert_message(resolved_text)
    assert len(alerts) == 1
    assert alerts[0].status == AlertStatus.RESOLVED


def test_parse_empty_or_invalid():
    assert parse_alert_message("") == []
    assert parse_alert_message("Some random text") == []
    assert parse_alert_message("Alerts Firing\nNo labels here") == []
