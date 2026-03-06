"""Alert parser — converts Alertmanager Telegram messages into Alert objects."""

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class Alert(BaseModel):
    """A single Prometheus alert extracted from a Telegram message."""

    status: AlertStatus
    alertname: str
    cluster: str
    namespace: str
    severity: str
    labels: dict[str, str]
    annotations: dict[str, str]
    fingerprint: str
    source_url: str | None = None

    @property
    def summary(self) -> str:
        return self.annotations.get("summary", "")

    @property
    def description(self) -> str:
        return self.annotations.get("description", "")

    @property
    def runbook_url(self) -> str | None:
        return self.annotations.get("runbook_url")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"^\s*-\s*(.+?)\s*=\s*(.+)$")
_SOURCE_RE = re.compile(r"^Source:\s*(\S+)$", re.MULTILINE)

# Headers sent by Alertmanager
_FIRING_HEADER = re.compile(r"alerts?\s+firing", re.IGNORECASE)
_RESOLVED_HEADER = re.compile(r"alerts?\s+resolved", re.IGNORECASE)


def _generate_fingerprint(labels: dict[str, str]) -> str:
    """Stable fingerprint from sorted label pairs (sha256 hex, first 16 chars)."""
    payload = "&".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _parse_kv_block(lines: list[str]) -> dict[str, str]:
    """Parse a block of ' - key = value' lines into a dict."""
    result: dict[str, str] = {}
    for line in lines:
        m = _LABEL_RE.match(line)
        if m:
            result[m.group(1).strip()] = m.group(2).strip()
    return result


def _split_into_alert_blocks(body: str) -> list[str]:
    """
    Split message body into individual alert blocks.
    Each block starts after a 'Labels:' heading.
    """
    # Split on 'Labels:' lines; keep delimiters
    parts = re.split(r"(?=^Labels:\s*$)", body, flags=re.MULTILINE)
    return [p.strip() for p in parts if p.strip().startswith("Labels:")]


def _parse_single_block(block: str, status: AlertStatus) -> Alert | None:
    """Parse one 'Labels: ... Annotations: ... Source: ...' block."""
    sections = re.split(r"^(Labels|Annotations):\s*$", block, flags=re.MULTILINE)

    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    source_url: str | None = None

    # sections alternates: [before_first_header, header, content, header, content, ...]
    i = 0
    while i < len(sections):
        token = sections[i].strip()
        if token == "Labels" and i + 1 < len(sections):
            labels = _parse_kv_block(sections[i + 1].splitlines())
            i += 2
        elif token == "Annotations" and i + 1 < len(sections):
            annotations = _parse_kv_block(sections[i + 1].splitlines())
            # Also look for Source: line in the same segment
            for line in sections[i + 1].splitlines():
                m = re.match(r"^Source:\s*(\S+)", line.strip())
                if m:
                    source_url = m.group(1)
            i += 2
        else:
            # Look for Source: line in body text parts
            sm = _SOURCE_RE.search(token)
            if sm:
                source_url = sm.group(1)
            i += 1

    if not labels:
        return None

    alertname = labels.get("alertname", "")
    if not alertname:
        return None

    fingerprint = _generate_fingerprint(labels)

    return Alert(
        status=status,
        alertname=alertname,
        cluster=labels.get("cluster", "unknown"),
        namespace=labels.get("namespace", ""),
        severity=labels.get("severity", ""),
        labels=labels,
        annotations=annotations,
        fingerprint=fingerprint,
        source_url=source_url,
    )


def parse_alert_message(text: str) -> list[Alert]:
    """
    Parse a Telegram message from Alertmanager into a list of Alert objects.

    Supports:
    - Multiple alerts in one message
    - Both 'Alerts Firing' and 'Alerts Resolved' headers
    - Standard Alertmanager Telegram template format

    Returns an empty list if the message is not an alert notification.
    """
    if not text:
        return []

    # Determine status from message header
    if _FIRING_HEADER.search(text):
        status = AlertStatus.FIRING
    elif _RESOLVED_HEADER.search(text):
        status = AlertStatus.RESOLVED
    else:
        return []  # Not an alert message

    # Remove the very first line (the header like "🚨 Alerts Firing:\n")
    lines = text.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        if _FIRING_HEADER.search(line) or _RESOLVED_HEADER.search(line):
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:])

    alerts: list[Alert] = []
    for block in _split_into_alert_blocks(body):
        alert = _parse_single_block(block, status)
        if alert:
            alerts.append(alert)

    return alerts
