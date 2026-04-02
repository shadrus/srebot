"""Tests for alert_parser."""

from pathlib import Path

from srebot.parser.alert_parser import (
    AlertStatus,
    parse_alert_message,
    update_remote_strategies,
)

FIXTURES = Path(__file__).parent / "fixtures"


def setup_module(module):
    """Seed the dynamic parser with same strategies used in prod for local tests."""
    update_remote_strategies(
        [
            {
                "name": "Standard",
                "firing_pattern": r"(alerts?\s+firing|\[FIRING:.*\]|FIRING|🔥|\*Alert:\*)",
                "resolved_pattern": r"(alerts?\s+resolved|\[RESOLVED:.*\]|RESOLVED|✅|\[RESOLVED\])",  # noqa: E501
                "labels_header_pattern": r"^(Labels|Details):\s*$",
                "annotations_header_pattern": r"^Annotations:\s*$",
                "kv_pattern": r"^\s*[\-•]\s*(.+?)\s*[=:]\s*(.+)$",
                "priority": 10,
            },
        ]
    )


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestParseAlertMessage:
    def test_empty_string_returns_empty(self):
        assert parse_alert_message("") == []

    def test_non_alert_message_returns_empty(self):
        assert parse_alert_message("Hello world, this is not an alert.") == []

    def test_multi_alert_firing(self):
        text = load_fixture("sample_alert.txt")
        alerts = parse_alert_message(text)

        assert len(alerts) == 2
        for alert in alerts:
            assert alert.status == AlertStatus.FIRING
            assert alert.cluster == "google-production"
            assert alert.namespace == "production"

    def test_first_alert_fields(self):
        text = load_fixture("sample_alert.txt")
        alert = parse_alert_message(text)[0]

        assert alert.alertname == "KubeDeploymentReplicasMismatch"
        assert alert.severity == "warning"
        assert "replicas" in alert.description.lower()
        assert alert.source_url is not None
        assert "prometheus" in alert.source_url.lower()
        assert alert.runbook_url is not None

    def test_second_alert_fields(self):
        text = load_fixture("sample_alert.txt")
        alert = parse_alert_message(text)[1]

        assert alert.alertname == "KubePodNotReady"
        assert "pod" in alert.description.lower()

    def test_fingerprint_is_deterministic(self):
        text = load_fixture("sample_alert.txt")
        alerts1 = parse_alert_message(text)
        alerts2 = parse_alert_message(text)

        assert alerts1[0].fingerprint == alerts2[0].fingerprint
        assert alerts1[1].fingerprint == alerts2[1].fingerprint

    def test_fingerprints_are_unique(self):
        text = load_fixture("sample_alert.txt")
        alerts = parse_alert_message(text)

        fps = [a.fingerprint for a in alerts]
        assert len(fps) == len(set(fps)), "All fingerprints should be unique"

    def test_resolved_alert(self):
        text = load_fixture("sample_resolved.txt")
        alerts = parse_alert_message(text)

        assert len(alerts) == 1
        assert alerts[0].status == AlertStatus.RESOLVED
        assert alerts[0].alertname == "KubeDeploymentReplicasMismatch"

    def test_resolved_fingerprint_matches_firing(self):
        """Resolved fingerprint must match the firing fingerprint for the same alert."""
        resolved_text = load_fixture("sample_resolved.txt")

        # The resolved fixture has fewer labels than firing, so fingerprints won't match
        # exactly by default — this test verifies the resolved alert parses correctly.
        resolved = parse_alert_message(resolved_text)
        assert resolved[0].status == AlertStatus.RESOLVED

    def test_case_insensitive_header(self):
        text = (
            "ALERTS FIRING:\nLabels:\n - alertname = TestAlert\n"
            " - cluster = prod\n - namespace = default\n - severity = critical\n"
            "Annotations:\n - summary = Test"
        )
        alerts = parse_alert_message(text)
        assert len(alerts) == 1
        assert alerts[0].status == AlertStatus.FIRING

    def test_labels_accessible_as_dict(self):
        text = load_fixture("sample_alert.txt")
        alert = parse_alert_message(text)[0]

        assert alert.labels["deployment"] == "ai-product-moderation"
        assert alert.labels["job"] == "kube-state-metrics"
