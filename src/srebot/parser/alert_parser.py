import hashlib
import logging
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AlertStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class Alert(BaseModel):
    """
    Structured alert data extracted from a raw message.
    """

    status: AlertStatus
    alertname: str
    cluster: str
    namespace: str = ""
    severity: str = ""
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    fingerprint: str = ""
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


_SOURCE_RE = re.compile(r"Source:\s*(\S+)", re.IGNORECASE)


class DynamicStrategy:
    def __init__(
        self,
        name: str,
        firing_pattern: str,
        resolved_pattern: str,
        labels_header_pattern: str,
        kv_pattern: str,
        annotations_header_pattern: str | None = None,
        priority: int = 10,
    ):
        self.name = name
        self.firing_re = re.compile(firing_pattern, re.IGNORECASE)
        self.resolved_re = re.compile(resolved_pattern, re.IGNORECASE)
        self.labels_header_re = re.compile(
            labels_header_pattern, re.IGNORECASE | re.MULTILINE
        )
        self.kv_re = re.compile(kv_pattern, re.IGNORECASE)
        self.annotations_header_re = (
            re.compile(annotations_header_pattern, re.IGNORECASE | re.MULTILINE)
            if annotations_header_pattern
            else None
        )
        self.priority = priority

    def _generate_fingerprint(self, labels: dict[str, str]) -> str:
        payload = "&".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _parse_kv_block(self, lines: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in lines:
            m = self.kv_re.match(line)
            if m:
                key = m.group(1).strip().strip("*").strip()
                val = m.group(2).strip().strip("`").strip()
                result[key] = val
        return result

    def parse(self, text: str) -> list[Alert]:
        # 1. Determine Status
        if self.resolved_re.search(text):
            status = AlertStatus.RESOLVED
        elif self.firing_re.search(text):
            status = AlertStatus.FIRING
        else:
            logger.debug("Strategy %s: No status header found", self.name)
            return []

        # 2. Split into blocks by labels header
        split_pattern = f"(?={self.labels_header_re.pattern})"
        parts = re.split(split_pattern, text, flags=re.MULTILINE | re.IGNORECASE)
        
        # Only take parts that actually start with the labels header
        blocks = [p.strip() for p in parts if self.labels_header_re.search(p)]

        if not blocks:
            logger.debug(
                "Strategy %s: Status %s found, but no blocks matched labels header",
                self.name,
                status,
            )
            return []

        results: list[Alert] = []
        for block in blocks:
            # Within each block, split into Labels and Annotations if exists
            labels, annotations = {}, {}
            source_url = None

            if self.annotations_header_re:
                sections = re.split(
                    f"({self.annotations_header_re.pattern})",
                    block,
                    flags=re.MULTILINE | re.IGNORECASE,
                )
                # sections: [labels_part, annotations_header, annotations_part]
                labels = self._parse_kv_block(sections[0].splitlines())
                if len(sections) > 2:
                    annotations = self._parse_kv_block(sections[2].splitlines())
                    source_url = self._extract_source(sections[2])
                if not source_url:
                    source_url = self._extract_source(sections[0])
            else:
                labels = self._parse_kv_block(block.splitlines())
                source_url = self._extract_source(block)

            if labels:
                results.append(
                    Alert(
                        status=status,
                        alertname=labels.get("alertname", "unknown"),
                        cluster=labels.get("cluster", "unknown"),
                        namespace=labels.get("namespace", ""),
                        severity=labels.get("severity", ""),
                        labels=labels,
                        annotations=annotations,
                        fingerprint=self._generate_fingerprint(labels),
                        source_url=source_url,
                    )
                )
            else:
                logger.debug("Strategy %s: Block found but no KV pairs matched", self.name)
        return results

    def _extract_source(self, text: str) -> str | None:
        m = _SOURCE_RE.search(text)
        return m.group(1) if m else None


# Global registry for strategies
_STRATEGIES: list[DynamicStrategy] = []


def update_remote_strategies(strategies_data: list[dict[str, Any]]) -> None:
    """Updates the global registry with strategies from the backend."""
    global _STRATEGIES
    new_strategies = []
    for s in strategies_data:
        try:
            new_strategies.append(DynamicStrategy(**s))
            logger.info("Loaded dynamic parsing strategy: %s", s.get("name"))
        except Exception as e:
            logger.error("Failed to load parsing strategy %s: %s", s.get("name"), e)
    
    # Sort by priority (lower number = higher priority)
    _STRATEGIES = sorted(new_strategies, key=lambda x: x.priority)


def parse_alert_message(text: str) -> list[Alert]:
    """
    Identifies if a message is an alert and parses it using the dynamic strategies waterfall.
    """
    if not text:
        return []

    for strategy in _STRATEGIES:
        try:
            alerts = strategy.parse(text)
            if alerts:
                logger.debug("Parsed alert using %s strategy", strategy.name)
                return alerts
        except Exception as e:
            logger.warning("Strategy %s failed: %s", strategy.name, e)

    return []
