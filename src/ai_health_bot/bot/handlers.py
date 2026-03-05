"""Telegram bot handlers — processes channel messages and orchestrates analysis."""

import asyncio
import logging

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ai_health_bot.llm.agent import get_agent
from ai_health_bot.parser.alert_parser import Alert, AlertStatus, parse_alert_message
from ai_health_bot.state.store import get_store

logger = logging.getLogger(__name__)


async def _handle_single_alert(alert: Alert, source_msg: Message) -> None:
    """Process one alert: dedup check → analyze → reply."""
    store = await get_store()
    agent = get_agent()

    if alert.status == AlertStatus.RESOLVED:
        already_tracked = await store.get_reply_message_id(alert.fingerprint)
        await store.mark_resolved(alert.fingerprint)
        logger.info("Resolved: %s [%s]", alert.alertname, alert.fingerprint)

        if already_tracked:
            try:
                await source_msg.reply_text(
                    f"✅ <b>Resolved:</b> <code>{alert.alertname}</code>\n"
                    f"<b>Cluster:</b> {alert.cluster} | <b>Namespace:</b> {alert.namespace}",
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=source_msg.message_id,
                )
            except Exception as exc:
                logger.warning("Could not send resolved reply: %s", exc)
        return

    # FIRING
    if not await store.is_new(alert.fingerprint):
        logger.info("Duplicate firing (skip): %s [%s]", alert.alertname, alert.fingerprint)
        return

    logger.info(
        "New firing alert: %s [%s] cluster=%s",
        alert.alertname,
        alert.fingerprint,
        alert.cluster,
    )

    # Send placeholder immediately so engineers see we're on it
    try:
        placeholder = await source_msg.reply_text(
            f"🔍 <b>Analyzing alert…</b>\n"
            f"<code>{alert.alertname}</code> · {alert.cluster} · {alert.namespace}",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=source_msg.message_id,
        )
    except Exception as exc:
        logger.error("Failed to send placeholder reply: %s", exc)
        return

    # Run LLM analysis
    try:
        analysis = await agent.analyze(alert)
    except Exception as exc:
        logger.exception("LLM analysis failed for %s", alert.fingerprint)
        analysis = f"⚠️ <b>Analysis failed:</b> <code>{exc}</code>\nPlease investigate manually."

    # Edit placeholder with full analysis
    try:
        await placeholder.edit_text(analysis, parse_mode=ParseMode.HTML)
    except Exception as exc:
        # If edit fails (e.g. message too old), send a new reply
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

    # Persist dedup state
    await store.mark_firing(alert.fingerprint, placeholder.message_id)


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for new channel posts.
    Parses Alertmanager messages and processes each contained alert.
    """
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return

    alerts = parse_alert_message(msg.text)
    if not alerts:
        logger.debug("Message %d: not an alert, skipping", msg.message_id)
        return

    logger.info("Parsed %d alert(s) from message %d", len(alerts), msg.message_id)

    # Handle alerts concurrently (one message can contain multiple)
    await asyncio.gather(
        *[_handle_single_alert(alert, msg) for alert in alerts],
        return_exceptions=True,
    )
