"""Telegram bot integration handlers — processes channel messages and orchestrates analysis."""

import asyncio
import hashlib
import logging
from collections import defaultdict

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from srebot.config import get_settings
from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, AlertStatus, parse_alert_message
from srebot.parser.filtering import get_ignore_registry
from srebot.state.store import get_store

logger = logging.getLogger(__name__)


def _group_key(alert: Alert) -> str:
    """
    Stable fingerprint for a group of related alerts.
    Alerts with the same alertname + cluster + job are treated as one problem.
    """
    job = alert.labels.get("job", "")
    payload = f"{alert.alertname}:{alert.cluster}:{job}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def _reply(source_msg: Message, text: str, dry_run: bool) -> Message | None:
    """Send a Telegram reply or, in dry_run mode, log it instead."""
    if dry_run:
        logger.info("[DRY-RUN] Would send Telegram message:\n%s", text)
        return None
    return await source_msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=source_msg.message_id,
    )


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    source_msg: Message,
) -> None:
    """
    Process a group of related alerts as a single analysis.
    All alerts share the same alertname + cluster + job.
    """
    dry_run = get_settings().dry_run
    store = await get_store()
    agent = get_agent()

    # Use the first alert as the representative for status/metadata
    primary = alerts[0]
    label = f"{primary.alertname} ({primary.cluster}/{primary.labels.get('job', '')})"

    # --- RESOLVED ---
    if primary.status == AlertStatus.RESOLVED:
        already_tracked = await store.get_reply_message_id(group_fp)
        await store.mark_resolved(group_fp)
        logger.info("Resolved group [%s]: %s", group_fp, label)

        if already_tracked:
            try:
                await _reply(
                    source_msg,
                    f"✅ <b>Resolved:</b> <code>{primary.alertname}</code>\n"
                    f"<b>Cluster:</b> {primary.cluster} | "
                    f"<b>Job:</b> {primary.labels.get('job', '—')}",
                    dry_run,
                )
            except Exception as exc:
                logger.warning("Could not send resolved reply: %s", exc)
        return

    # --- FIRING ---
    if not await store.is_new(group_fp):
        logger.info("Duplicate firing group (skip): %s [%s]", label, group_fp)
        return

    logger.info("New firing group [%s]: %s (%d alert(s))", group_fp, label, len(alerts))

    # Send placeholder
    placeholder = None
    if not dry_run:
        try:
            placeholder = await source_msg.reply_text(
                f"🔍 <b>Analyzing {len(alerts)} alert(s)…</b>\n"
                f"<code>{primary.alertname}</code> · "
                f"{primary.cluster} · {primary.labels.get('job', '')}",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=source_msg.message_id,
            )
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return
    else:
        logger.info(
            "[DRY-RUN] Analyzing group %s: %d alert(s) (no Telegram placeholder sent)",
            group_fp,
            len(alerts),
        )

    # Run LLM analysis
    try:
        analysis = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ <b>Analysis failed:</b> <code>{exc}</code>\nPlease investigate manually."

    if dry_run:
        logger.info("[DRY-RUN] Analysis result for group %s:\n%s", group_fp, analysis)
        await store.mark_firing(group_fp, reply_message_id=0)
        return

    # Edit placeholder with full analysis
    try:
        await placeholder.edit_text(analysis, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Could not edit placeholder (%s), sending new message", exc)
        try:
            await source_msg.reply_text(
                analysis,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=source_msg.message_id,
            )
        except Exception as exc2:
            logger.error("Could not send analysis reply: %s", exc2)
            return

    await store.mark_firing(group_fp, placeholder.message_id)


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for new channel posts.

    Parses Alertmanager messages, filters ignored alerts, groups by
    (alertname, cluster, job) and runs one analysis per group.
    """
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return

    logger.debug("Received message %d from chat_id=%d", msg.message_id, msg.chat_id)

    alerts = parse_alert_message(msg.text)
    if not alerts:
        logger.debug("Message %d: not an alert, skipping", msg.message_id)
        return

    logger.info("Parsed %d alert(s) from message %d", len(alerts), msg.message_id)

    # Apply ignore rules
    registry = get_ignore_registry()
    active_alerts = [a for a in alerts if not registry.should_ignore(a)]
    ignored = len(alerts) - len(active_alerts)
    if ignored:
        logger.info("Ignored %d alert(s) by rules, %d remaining", ignored, len(active_alerts))
    if not active_alerts:
        return

    # Group by alertname + cluster + job
    groups: dict[str, list[Alert]] = defaultdict(list)
    for alert in active_alerts:
        groups[_group_key(alert)].append(alert)

    logger.info("Grouped %d alert(s) into %d group(s)", len(active_alerts), len(groups))

    results = await asyncio.gather(
        *[_handle_alert_group(fp, group, msg) for fp, group in groups.items()],
        return_exceptions=True,
    )
    for (fp, group), result in zip(groups.items(), results):
        if isinstance(result, BaseException):
            logger.exception(
                "Unhandled error processing group %s (%s): %s",
                fp,
                group[0].alertname,
                result,
                exc_info=result,
            )
