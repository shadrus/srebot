"""Telegram bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from srebot.bot.shared import RejectionReason, handle_followup_question, process_alert_text
from srebot.bot.telegram.html_utils import markdown_to_telegram_html
from srebot.config import get_settings
from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, AlertStatus
from srebot.state.store import get_store

logger = logging.getLogger(__name__)


MESSAGES = {
    "Russian": {
        "analyzing_alerts": "🔍 <b>Анализирую {count} алерт(ов)…</b>",
        "ttl_footer": "\n\n<i>💬 Задайте уточняющие вопросы ответом на это сообщение в течение {hours} ч.</i>",  # noqa: E501
        "analyzing_followup": "🔍 <i>Анализирую...</i>",
        "cooldown": "⏳ <i>Подождите немного перед следующим вопросом.</i>",
        "limit_reached": "🔒 <i>Лимит уточняющих вопросов по этому инциденту исчерпан ({current}/{max}).</i>",  # noqa: E501
    },
    "English": {
        "analyzing_alerts": "🔍 <b>Analyzing {count} alert(s)…</b>",
        "ttl_footer": "\n\n<i>💬 Ask follow-up questions by replying to this message within {hours} h.</i>",  # noqa: E501
        "analyzing_followup": "🔍 <i>Analyzing...</i>",
        "cooldown": "⏳ <i>Please wait a bit before the next question.</i>",
        "limit_reached": "🔒 <i>Limit of follow-up questions for this incident reached ({current}/{max}).</i>",  # noqa: E501
    },
}


def get_msg(key: str) -> str:
    lang = get_settings().llm_response_language
    return MESSAGES.get(lang, MESSAGES["English"]).get(key, "")


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
                get_msg("analyzing_alerts").format(count=len(alerts)) + "\n"
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
        analysis, incident_id = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ <b>Analysis failed:</b> <code>{exc}</code>\nPlease investigate manually."
        incident_id = None

    # Check if the alert was resolved while we were analyzing
    current_status = await store.get_status(group_fp)

    if dry_run:
        logger.info("[DRY-RUN] Analysis result for group %s:\n%s", group_fp, analysis)
        if current_status == "analyzing":
            await store.mark_firing(group_fp, reply_message_id=0)
        return

    # ALWAYS update the UI with findings, even if already resolved
    safe_analysis = markdown_to_telegram_html(analysis)

    # Append TTL footer so engineers know how long the follow-up window is open
    ttl_hours = get_settings().followup_ttl // 3600
    safe_analysis += get_msg("ttl_footer").format(hours=ttl_hours)

    try:
        await placeholder.edit_text(safe_analysis, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Could not edit placeholder (%s), sending new message", exc)
        try:
            await source_msg.reply_text(
                safe_analysis,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=source_msg.message_id,
            )
        except Exception as exc2:
            logger.error(
                "Could not send analysis reply with HTML (%s), falling back to plain text",  # noqa: E501
                exc2,
            )
            try:
                # Final fallback: send raw text without any formatting
                await source_msg.reply_text(
                    analysis,
                    parse_mode=None,
                    reply_to_message_id=source_msg.message_id,
                )
            except Exception as exc3:
                logger.error("Total failure sending analysis reply: %s", exc3)
                return

    # Only restore FIRING state if it wasn't cleared by a RESOLVED message
    if current_status == "analyzing":
        await store.mark_firing(group_fp, placeholder.message_id)
        # Save follow-up context for thread replies
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
        await store.save_followup_context(group_fp, analysis, alert_data, incident_id=incident_id)
        await store.register_bot_message(placeholder.message_id, group_fp)
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


async def followup_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for reply messages in the bot's group thread.

    Detects replies to bot RCA messages and routes them as follow-up
    questions to the SaaS backend with the original incident context.
    """
    msg = update.message or update.channel_post
    if not msg or not msg.text or not msg.reply_to_message:
        return

    # Ignore messages from bots (including ourselves)
    if msg.from_user and msg.from_user.is_bot:
        return

    dry_run = get_settings().dry_run
    reply_to_id = str(msg.reply_to_message.message_id)
    user_id = str(msg.from_user.id) if getattr(msg, "from_user", None) else f"channel_{msg.chat_id}"
    question = msg.text.strip()

    if not question:
        return

    # Check for commands
    from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

    if is_command_message(question):
        chat_id = extract_chat_id(msg)
        if chat_id:
            response = await handle_command(
                question, reply_to_id, chat_id, bot_username=context.bot.username
            )
            if response:
                await _reply(msg, markdown_to_telegram_html(response), dry_run)
                return

    logger.debug(
        "Follow-up reply from user=%s to message=%s: %.80s",
        user_id,
        reply_to_id,
        question,
    )

    indicator = None
    if not dry_run:
        try:
            indicator = await msg.reply_text(
                get_msg("analyzing_followup"),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg.message_id,
            )
        except Exception as exc:
            logger.warning("Could not send follow-up typing indicator: %s", exc)

    answer, rejection = await handle_followup_question(
        reply_to_id=reply_to_id,
        question=question,
        user_id=user_id,
    )

    if rejection is not None:
        if rejection == RejectionReason.NO_CONTEXT:
            # Silently ignore — reply is not to a bot RCA message
            if indicator:
                try:
                    await indicator.delete()
                except Exception as exc:
                    logger.warning("Could not delete follow-up indicator: %s", exc)
            return
        if rejection == RejectionReason.COOLDOWN:
            user_msg = get_msg("cooldown")
        else:  # LIMIT_REACHED
            max_turns = get_settings().followup_max_turns
            user_msg = get_msg("limit_reached").format(current=max_turns, max=max_turns)

        if indicator:
            try:
                await indicator.edit_text(user_msg, parse_mode=ParseMode.HTML)
            except Exception as exc:
                logger.warning("Could not edit follow-up rejection message: %s", exc)
        else:
            if not dry_run:
                try:
                    await msg.reply_text(
                        user_msg,
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg.message_id,
                    )
                except Exception as exc:
                    logger.warning("Could not send follow-up rejection message: %s", exc)
            else:
                logger.info("[DRY-RUN] Follow-up rejection (%s): %s", rejection.value, user_msg)
        return

    if dry_run:
        logger.info("[DRY-RUN] Follow-up answer for user=%s:\n%s", user_id, answer)
        return

    # Success path
    if indicator:
        try:
            safe_answer = markdown_to_telegram_html(answer)
            await indicator.edit_text(safe_answer, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Could not edit follow-up answer: %s", exc)
            try:
                await msg.reply_text(
                    markdown_to_telegram_html(answer),
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=msg.message_id,
                )
            except Exception as exc2:
                logger.error("Total failure sending follow-up answer: %s", exc2)
    else:
        try:
            await msg.reply_text(
                markdown_to_telegram_html(answer),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg.message_id,
            )
        except Exception as exc:
            logger.error("Failure sending follow-up answer (no indicator): %s", exc)


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for new channel posts.

    Parses Alertmanager messages, filters ignored alerts, groups by
    (alertname, cluster, job) and runs one analysis per group.
    """
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return

    # Ignore replies (handled by followup_reply_handler in a different group)
    if msg.reply_to_message:
        return

    logger.debug("Received message %d from chat_id=%d", msg.message_id, msg.chat_id)

    # Check for commands
    from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

    if is_command_message(msg.text):
        chat_id = extract_chat_id(msg)
        if chat_id:
            dry_run = get_settings().dry_run
            response = await handle_command(
                msg.text, None, chat_id, bot_username=context.bot.username
            )
            if response:
                await _reply(msg, markdown_to_telegram_html(response), dry_run)
                return

    await process_alert_text(msg.text, _handle_alert_group, msg)
