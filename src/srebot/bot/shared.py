"""
Shared helpers used by all bot platform integrations.

Platform-specific handlers delegate the common alert-processing pipeline
(parse → filter → group → gather) here so it is implemented exactly once.
"""

import asyncio
import enum
import hashlib
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable

import srebot.config as config
import srebot.llm.agent as llm_agent
import srebot.state.store as state_store
from srebot.bot.delivery import DeliveryReceipt
from srebot.parser.alert_parser import Alert, AlertStatus, parse_alert_message
from srebot.parser.filtering import get_ignore_registry
from srebot.state.store import (
    FollowupAdmission,
    conversation_identity,
    is_usable_followup_context,
)

logger = logging.getLogger(__name__)


class RejectionReason(enum.Enum):
    """Reason why a follow-up request was rejected."""

    NO_CONTEXT = "no_context"  # No RCA context found — not a bot reply
    COOLDOWN = "cooldown"  # User is sending too fast
    LIMIT_REACHED = "limit_reached"  # Max turns exhausted for this incident
    USER_LIMIT_REACHED = "user_limit_reached"
    INCIDENT_LIMIT_REACHED = "incident_limit_reached"


def rejection_turn_limit(settings: config.Settings, rejection: RejectionReason) -> int:
    """Return the quota value to display for a structured rejection."""
    if rejection == RejectionReason.INCIDENT_LIMIT_REACHED:
        return settings.followup_incident_max_turns
    return settings.effective_followup_user_max_turns


class ChatAdapter(ABC):
    @abstractmethod
    async def send_resolved(
        self, label: str, primary: Alert, current_status: str, reply_to_id: str | None
    ) -> None:
        """Called when an alert group is resolved."""
        pass

    @abstractmethod
    async def send_short_notification(
        self, group_fp: str, label: str, primary: Alert
    ) -> DeliveryReceipt:
        """Post a short notification and return every delivered message ID."""
        pass

    @abstractmethod
    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> DeliveryReceipt:
        """Post the analyzing placeholder and return every delivered message ID."""
        pass

    @abstractmethod
    async def update_with_analysis(
        self,
        group_fp: str,
        placeholder_id: str | int | None,
        analysis: str,
        is_billing_error: bool,
    ) -> DeliveryReceipt:
        """Update the UI and return every successfully displayed message ID."""
        pass

    @abstractmethod
    def get_chat_id(self) -> str:
        """Returns the chat ID associated with this adapter for state tracking."""
        pass


async def register_followup_receipt(
    receipt: DeliveryReceipt,
    fingerprint: str | None,
    incident_id: str | None,
    chat_id: str,
    *,
    additional_message_ids: tuple[str | int, ...] = (),
) -> None:
    """Associate every delivered follow-up message with its conversation context.

    Args:
        receipt: Delivery result containing all successfully delivered message IDs.
        fingerprint: Follow-up fingerprint returned by the shared workflow.
        incident_id: Newly created incident ID, if the analysis produced one.
        chat_id: Namespaced platform chat identifier.
        additional_message_ids: Existing thread roots that should resolve to the same context.
    """
    if not receipt.message_ids or not fingerprint:
        return

    store = await state_store.get_store()
    resolved_incident_id = incident_id
    if resolved_incident_id is None and fingerprint != "general_query":
        context = await store.get_followup_context(fingerprint)
        if isinstance(context, dict):
            parent_incident_id = context.get("incident_id")
            if isinstance(parent_incident_id, str) and parent_incident_id:
                resolved_incident_id = parent_incident_id

    message_ids = dict.fromkeys(
        (*receipt.message_ids, *(str(message_id) for message_id in additional_message_ids))
    )
    for message_id in message_ids:
        await store.register_bot_message(
            message_id,
            fingerprint,
            incident_id=resolved_incident_id,
        )
    await store.set_last_active_incident(chat_id, fingerprint)


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


async def execute_alert_group_workflow(
    group_fp: str,
    alerts: list[Alert],
    adapter: ChatAdapter,
    dry_run: bool,
) -> None:
    """
    Unified workflow for handling an alert group.
    Applies to Telegram, Slack, and Discord via the ChatAdapter.
    """

    store = await state_store.get_store()
    agent = llm_agent.get_agent()

    primary = alerts[0]
    label = f"{primary.alertname} ({primary.cluster}/{primary.labels.get('job', '')})"

    # --- RESOLVED ---
    if primary.status == AlertStatus.RESOLVED:
        current_status = await store.get_status(group_fp)
        reply_to_id = await store.get_reply_message_id(group_fp)

        await store.mark_resolved(group_fp)
        logger.info("Resolved group [%s]: %s (was %s)", group_fp, label, current_status)

        if current_status in ("firing", "analyzing") or reply_to_id:
            await adapter.send_resolved(label, primary, current_status, reply_to_id)
        return

    # --- FIRING ---
    if not await store.is_new(group_fp):
        logger.info("Duplicate firing group (skip): %s [%s]", label, group_fp)
        return

    logger.info("New firing group [%s]: %s (%d alert(s))", group_fp, label, len(alerts))

    alert_data = [
        {
            "status": a.status,
            "alertname": a.alertname,
            "cluster": a.cluster,
            "namespace": a.namespace,
            "severity": a.severity,
            "labels": a.labels,
            "annotations": a.annotations,
            "fingerprint": a.fingerprint,
            "source_url": a.source_url,
        }
        for a in alerts
    ]

    chat_id = adapter.get_chat_id()

    # Short Notification (Auto-analysis disabled)
    if not config.get_settings().auto_analyze_alerts:
        logger.info("Auto-analysis disabled, posting short notification for %s", group_fp)
        receipt = await adapter.send_short_notification(group_fp, label, primary)
        if not dry_run and receipt.primary_message_id is not None:
            await store.mark_firing(group_fp, receipt.primary_message_id)
            await store.save_followup_context(group_fp, "", alert_data, incident_id=None)
            for message_id in receipt.message_ids:
                await store.register_bot_message(message_id, group_fp, incident_id=None)
            await store.set_last_active_incident(chat_id, group_fp)
        elif dry_run:
            await store.mark_firing(group_fp, "0")
        return

    # Send analyzing placeholder
    placeholder_receipt = await adapter.send_analyzing_placeholder(
        group_fp, label, primary, len(alerts)
    )
    placeholder_id = placeholder_receipt.primary_message_id
    if not dry_run and placeholder_id is not None:
        await store.mark_analyzing(group_fp, placeholder_id)
    elif dry_run:
        await store.mark_analyzing(group_fp, "0")

    # Run LLM analysis
    try:
        analysis, incident_id = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ Analysis failed: {exc}\nPlease investigate manually."
        incident_id = None

    is_billing_error = (
        "insufficient balance" in analysis.lower() or "недостаточно баланса" in analysis.lower()
    )

    current_status = await store.get_status(group_fp)

    if dry_run:
        logger.info("[DRY-RUN] Analysis result for group %s:\n%s", group_fp, analysis)
        if current_status == "analyzing":
            await store.mark_firing(group_fp, reply_message_id=0)
        return

    # Update UI with final analysis
    receipt = await adapter.update_with_analysis(
        group_fp, placeholder_id, analysis, is_billing_error
    )

    if receipt.primary_message_id is not None:
        if current_status == "analyzing":
            await store.save_followup_context(
                group_fp, analysis, alert_data, incident_id=incident_id
            )
            delivered_message_ids = dict.fromkeys(
                (*placeholder_receipt.message_ids, *receipt.message_ids)
            )
            for message_id in delivered_message_ids:
                await store.register_bot_message(message_id, group_fp, incident_id=incident_id)
            await store.set_last_active_incident(chat_id, group_fp)
            await store.mark_firing(group_fp, receipt.primary_message_id)
        elif current_status == "resolved":
            logger.info(
                "Alert %s was resolved during analysis. "
                "Not marking as firing or saving followup context.",
                group_fp,
            )
            await store.mark_resolved(group_fp)


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
        agent = llm_agent.get_agent()
        alerts = await agent.parse_raw_text(text)

    if not alerts:
        logger.debug("No alerts found in message after both regex and smart parsing")
        return

    logger.info("Parsed %d alert(s)", len(alerts))

    registry = get_ignore_registry()
    active_alerts = [a for a in alerts if not registry.should_ignore(a)]

    # Filter out muted alerts
    from srebot.bot.commands import extract_chat_id

    chat_id = extract_chat_id(*handler_args)
    if chat_id:
        store = await state_store.get_store()
        unmuted_alerts = []
        for a in active_alerts:
            if await store.is_muted(chat_id, a.alertname):
                logger.info("Alert %s skipped because it is muted in chat %s", a.alertname, chat_id)
            else:
                unmuted_alerts.append(a)
        active_alerts = unmuted_alerts

    ignored = len(alerts) - len(active_alerts)
    if ignored:
        logger.info("Ignored/muted %d alert(s), %d remaining", ignored, len(active_alerts))
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
    reply_to_id: str | None,
    question: str,
    user_id: str,
    chat_id: str | None = None,
    allowed_servers: list[str] | None = None,
    user_display_name: str | None = None,
    on_tool_failure: Callable[[list[str]], Awaitable[None]] | None = None,
) -> tuple[str, str | None, str | None, RejectionReason | None]:
    """
    Common follow-up processing pipeline shared by all platform integrations.

    Validates the reply or mention, enforces rate limits and turn caps, then calls the LLM
    with the previous RCA context or initiates a general query.

    Args:
        reply_to_id: ID of the message being replied to, or None if direct mention.
        question: The engineer's follow-up question text.
        user_id: Platform user identifier (for rate limiting).
        chat_id: The chat identifier (to resolve active incident context for mentions).
        allowed_servers: MCP server names allowed for this cluster (passed to agent).
        on_tool_failure: Optional callback invoked when MCP data sources fail.

    Returns:
        Tuple of (answer, new_incident_id, fingerprint_used, rejection_reason).
        If rejection_reason is not None, answer is an empty string and the caller
        should surface a platform-specific user-facing message based on the
        rejection reason.
    """

    store = await state_store.get_store()
    settings = config.get_settings()

    fp = None
    parent_incident_id = None
    rca_text = ""
    alert_data = []

    if reply_to_id:
        msg_ctx = await store.get_bot_message_context(reply_to_id)
        if msg_ctx:
            fp = msg_ctx["fingerprint"]
            parent_incident_id = msg_ctx["incident_id"]
        else:
            logger.debug("Follow-up: reply %s has no bot context", reply_to_id)
            return "", None, None, RejectionReason.NO_CONTEXT

    if fp and fp != "general_query":
        ctx = await store.get_followup_context(fp)
        if not is_usable_followup_context(ctx):
            logger.debug("Follow-up: context missing or invalid for group %s", fp)
            return "", None, fp, RejectionReason.NO_CONTEXT
        identity = conversation_identity(chat_id, user_id)
        admission = await store.admit_followup(fp, identity)
        admission_rejections = {
            FollowupAdmission.COOLDOWN: RejectionReason.COOLDOWN,
            FollowupAdmission.USER_LIMIT: RejectionReason.USER_LIMIT_REACHED,
            FollowupAdmission.INCIDENT_LIMIT: RejectionReason.INCIDENT_LIMIT_REACHED,
            FollowupAdmission.NO_CONTEXT: RejectionReason.NO_CONTEXT,
        }
        if admission != FollowupAdmission.ACCEPTED:
            rejection = admission_rejections[admission]
            logger.info(
                "Follow-up rejected reason=%s user=%s group=%s",
                rejection.value,
                identity.key,
                fp,
            )
            return "", None, fp, rejection
        rca_text = ctx["rca_text"]
        alert_data = ctx["alert_data"]
    else:
        # General query session: no active incident context
        identity = conversation_identity(chat_id, user_id)
        if await store.check_and_set_scoped_cooldown(identity):
            logger.info("General query rejected reason=cooldown user=%s", identity.key)
            return "", None, None, RejectionReason.COOLDOWN
        fp = "general_query"
        rca_text = ""
        alert_data = []
        allowed_servers = None  # General queries can use all tools

    agent = llm_agent.get_agent()
    try:
        followup_kwargs = {
            "question": question,
            "rca_text": rca_text,
            "alert_data": alert_data,
            "allowed_servers": allowed_servers,
            "incident_scoped": fp != "general_query",
            "parent_incident_id": parent_incident_id,
            "user_name": user_display_name,
        }
        if on_tool_failure:
            followup_kwargs["on_tool_failure"] = on_tool_failure
        answer, new_incident_id = await agent.followup(**followup_kwargs)
    except Exception:
        logger.exception("Unhandled failure during follow-up analysis")
        if settings.llm_response_language == "Russian":
            answer = (
                "⚠️ <b>Не удалось завершить анализ.</b> Произошла внутренняя ошибка; "
                "попробуйте повторить запрос позже."
            )
        else:
            answer = (
                "⚠️ <b>Could not complete the analysis.</b> An internal error occurred; "
                "please try again later."
            )
        new_incident_id = None

    if fp != "general_query":
        if new_incident_id:
            await store.update_followup_context_incident_id(fp, new_incident_id)
        if not rca_text:
            await store.update_followup_context_rca_text(fp, answer)

    return answer, new_incident_id, fp, None
