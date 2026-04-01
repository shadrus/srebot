"""
Shared helpers used by all bot platform integrations.

Platform-specific handlers delegate the common alert-processing pipeline
(parse → filter → group → gather) here so it is implemented exactly once.
"""

import asyncio
import hashlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, parse_alert_message
from srebot.parser.filtering import get_ignore_registry

logger = logging.getLogger(__name__)


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
