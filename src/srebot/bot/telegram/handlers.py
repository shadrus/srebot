"""Telegram bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from srebot.bot.shared import process_alert_text
from srebot.config import get_settings
from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, AlertStatus
from srebot.state.store import get_store

logger = logging.getLogger(__name__)


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
        current_status = await store.get_status(group_fp)
        reply_to_id = await store.get_reply_message_id(group_fp)

        await store.mark_resolved(group_fp)
        logger.info("Resolved group [%s]: %s (was %s)", group_fp, label, current_status)

        # Notify if we were tracking this alert (firing or analyzing)
        if current_status in ("firing", "analyzing") or reply_to_id:
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
            # Mark as ANALYZING so concurrent RESOLVED messages know we are on it
            await store.mark_analyzing(group_fp, placeholder.message_id)
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return
    else:
        logger.info(
            "[DRY-RUN] Analyzing group %s: %d alert(s) (no Telegram placeholder sent)",
            group_fp,
            len(alerts),
        )
        await store.mark_analyzing(group_fp, 0)

    # Run LLM analysis
    try:
        analysis = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ <b>Analysis failed:</b> <code>{exc}</code>\nPlease investigate manually."

    # Check if the alert was resolved while we were analyzing
    current_status = await store.get_status(group_fp)

    if dry_run:
        logger.info("[DRY-RUN] Analysis result for group %s:\n%s", group_fp, analysis)
        if current_status == "analyzing":
            await store.mark_firing(group_fp, reply_message_id=0)
        return

    # ALWAYS update the UI with findings, even if already resolved
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

    # Only restore FIRING state if it wasn't cleared by a RESOLVED message
    if current_status == "analyzing":
        await store.mark_firing(group_fp, placeholder.message_id)
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


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
    await process_alert_text(msg.text, _handle_alert_group, msg)
