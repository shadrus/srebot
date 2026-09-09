"""Domain events for user-visible analysis progress."""

import enum
from dataclasses import dataclass


class ProgressPhase(enum.StrEnum):
    """Confirmed analysis phase visible to chat integrations."""

    TOOL_EXECUTION = "tool_execution"
    ANALYZING_RESULTS = "analyzing_results"
    PARTIAL_RESULTS = "partial_results"
    UNAVAILABLE_RESULTS = "unavailable_results"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A confirmed progress transition emitted by the analysis client."""

    phase: ProgressPhase
    public_status: str | None = None
