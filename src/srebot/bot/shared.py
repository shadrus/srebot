"""
Shared helpers used by all bot platform integrations.

Platform-specific handlers delegate the common alert-processing pipeline
(parse → filter → group → gather) here so it is implemented exactly once.
"""

import asyncio
import enum
import hashlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, parse_alert_message
from srebot.parser.filtering import get_ignore_registry

logger = logging.getLogger(__name__)


class RejectionReason(enum.Enum):
    """Reason why a follow-up request was rejected."""

    NO_CONTEXT = "no_context"  # No RCA context found — not a bot reply
    COOLDOWN = "cooldown"  # User is sending too fast
    LIMIT_REACHED = "limit_reached"  # Max turns exhausted for this incident


def group_key(alert: Alert) -> str:
    """
    Stable fingerprint for a group of related alerts.

    Alerts with the same alertname + cluster + job are treated as one problem.

    Args:
        alert: Parsed alert object.

    Returns:
        16-character hex string derived from alertname, cluster, and job.
    """
    job = alert.labels.get("job", "")
    payload = f"{alert.alertname}:{alert.cluster}:{job}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def process_alert_text(
    text: str,
    handle_group: Callable[..., Awaitable[None]],
    *handler_args,
) -> None:
    """
    Common alert-processing pipeline shared by all platform integrations.

    Parses *text*, filters ignored alerts, groups survivors by
    ``(alertname, cluster, job)`` and calls *handle_group* concurrently
    for every group.

    Args:
        text: Raw message text potentially containing Alertmanager payload.
        handle_group: Async callable invoked for each alert group.
            Signature: ``handle_group(group_fp, alerts, *handler_args)``.
        *handler_args: Extra positional arguments forwarded to *handle_group*
            after ``group_fp`` and ``alerts`` (e.g. channel id, API client).
    """
    alerts = parse_alert_message(text)
    if not alerts:
        # Fallback to Intelligent Parsing via SaaS Agent
        logger.info("Regex parsing failed, trying smart parsing via Agent...")
        agent = get_agent()
        alerts = await agent.parse_raw_text(text)

    if not alerts:
        logger.debug("No alerts found in message after both regex and smart parsing")
        return

    logger.info("Parsed %d alert(s)", len(alerts))

    registry = get_ignore_registry()
    active_alerts = [a for a in alerts if not registry.should_ignore(a)]
    ignored = len(alerts) - len(active_alerts)
    if ignored:
        logger.info("Ignored %d alert(s) by rules, %d remaining", ignored, len(active_alerts))
    if not active_alerts:
        return

    groups: dict[str, list[Alert]] = defaultdict(list)
    for alert in active_alerts:
        groups[group_key(alert)].append(alert)

    logger.info("Grouped %d alert(s) into %d group(s)", len(active_alerts), len(groups))

    results = await asyncio.gather(
        *[handle_group(fp, grp, *handler_args) for fp, grp in groups.items()],
        return_exceptions=True,
    )
    for (fp, grp), result in zip(groups.items(), results):
        if isinstance(result, BaseException):
            logger.exception(
                "Unhandled error processing group %s (%s): %s",
                fp,
                grp[0].alertname,
                result,
                exc_info=result,
            )


async def handle_followup_question(
    reply_to_id: str,
    question: str,
    user_id: str,
    allowed_servers: list[str] | None = None,
) -> tuple[str, RejectionReason | None]:
    """
    Common follow-up processing pipeline shared by all platform integrations.

    Validates the reply, enforces rate limits and turn caps, then calls the LLM
    with the previous RCA context.

    Args:
        reply_to_id: ID of the message being replied to (str for cross-platform).
        question: The engineer's follow-up question text.
        user_id: Platform user identifier (for rate limiting).
        allowed_servers: MCP server names allowed for this cluster (passed to agent).

    Returns:
        Tuple of (answer, rejection_reason). If rejection_reason is not None,
        answer is an empty string and the caller should surface a platform-specific
        user-facing message based on the rejection reason.
    """
    from srebot.config import get_settings
    from srebot.state.store import get_store

    store = await get_store()
    settings = get_settings()

    fp = await store.get_fp_by_message_id(reply_to_id)
    if not fp:
        logger.debug("Follow-up: no context found for message_id=%s — ignoring", reply_to_id)
        return "", RejectionReason.NO_CONTEXT

    in_cooldown = await store.check_and_set_user_cooldown(user_id)
    if in_cooldown:
        logger.debug("Follow-up: user %s is in cooldown", user_id)
        return "", RejectionReason.COOLDOWN

    turns = await store.increment_followup_turns(fp)
    if turns > settings.followup_max_turns:
        logger.info(
            "Follow-up: max turns (%d) exceeded for group %s (turn %d)",
            settings.followup_max_turns,
            fp,
            turns,
        )
        return "", RejectionReason.LIMIT_REACHED

    ctx = await store.get_followup_context(fp)
    if not ctx:
        logger.debug("Follow-up: context expired for group %s", fp)
        return "", RejectionReason.NO_CONTEXT

    agent = get_agent()
    answer = await agent.followup(
        question=question,
        rca_text=ctx["rca_text"],
        alert_data=ctx["alert_data"],
        allowed_servers=allowed_servers,
        parent_incident_id=ctx.get("incident_id"),
    )
    return answer, None
