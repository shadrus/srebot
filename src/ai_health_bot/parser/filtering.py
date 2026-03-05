"""Alert filtering logic — supports nested AND/OR rules based on labels."""

import logging

from pydantic import BaseModel, Field

from ai_health_bot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)

class FilterCondition(BaseModel):
    """
    A single condition or a group of conditions.
    If 'labels' is set, it matches if all labels match (AND).
    If 'any' is set, it matches if ANY sub-condition matches (OR).
    If 'all' is set, it matches if ALL sub-conditions match (AND).
    """
    labels: dict[str, str] = Field(default_factory=dict)
    any: list["FilterCondition"] = Field(default_factory=list)
    all: list["FilterCondition"] = Field(default_factory=list)

    def matches(self, alert: Alert) -> bool:
        # Check direct labels (AND logic)
        for k, v in self.labels.items():
            if alert.labels.get(k) != v:
                return False
        
        # Check 'all' sub-conditions (AND logic)
        for cond in self.all:
            if not cond.matches(alert):
                return False
        
        # Check 'any' sub-conditions (OR logic)
        if self.any:
            found_match = False
            for cond in self.any:
                if cond.matches(alert):
                    found_match = True
                    break
            if not found_match:
                return False
        
        # If we have no labels, no 'any', and no 'all', it's an empty condition.
        # Decisions: empty condition = no match? Or should we avoid empty conditions?
        # For now, if at least one check was performed, we returned.
        # If no checks were performed but the object exists, we'll return True if it's not empty.
        if not self.labels and not self.any and not self.all:
             return False

        return True

class IgnoreRule(BaseModel):
    name: str = "Unnamed Rule"
    condition: FilterCondition

class IgnoreRegistry:
    def __init__(self, rules: list[IgnoreRule]) -> None:
        self._rules = rules

    def should_ignore(self, alert: Alert) -> bool:
        for rule in self._rules:
            if rule.condition.matches(alert):
                logger.info(
                    "Alert %s [%s] ignored by rule: %s",
                    alert.alertname,
                    alert.fingerprint,
                    rule.name,
                )
                return True
        return False

# Module-level singleton helper
_ignore_registry: IgnoreRegistry | None = None

def get_ignore_registry() -> IgnoreRegistry:
    global _ignore_registry
    if _ignore_registry is None:
        from ai_health_bot.config import get_settings
        s = get_settings()
        _ignore_registry = IgnoreRegistry(s.ignore_rules)
    return _ignore_registry
