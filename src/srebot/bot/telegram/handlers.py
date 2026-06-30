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
    is_billing_error = (
        "insufficient balance" in analysis.lower() or "недостаточно баланса" in analysis.lower()
    )
    if not is_billing_error:
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
        await store.register_bot_message(placeholder.message_id, group_fp, incident_id=incident_id)
        await store.set_last_active_incident(str(source_msg.chat_id), group_fp)
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


def clean_mentions(text: str, bot_username: str | None, bot_first_name: str | None) -> str:
    """Remove bot username mentions and display name mentions from the message."""
    import re

    cleaned = text
    if bot_username and isinstance(bot_username, str):
        # Match @bot_username or bot_username (case-insensitive) as a whole word
        pattern = re.compile(rf"(?i)@?{re.escape(bot_username)}\b")
        cleaned = pattern.sub("", cleaned)
    if bot_first_name and isinstance(bot_first_name, str):
        # Match bot_first_name (case-insensitive) as a whole word
        pattern = re.compile(rf"(?i)\b{re.escape(bot_first_name)}\b")
        cleaned = pattern.sub("", cleaned)

    # Strip any leading/trailing commas, colons, semicolons, and spaces
    cleaned = re.sub(r"^[,\s:;?]+|[,\s:;?]+$", "", cleaned).strip()
    return cleaned


async def followup_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for replies and mentions in the bot's group thread.

    Detects replies to bot RCA messages or direct bot mentions and routes them as follow-up
    questions to the SaaS backend with the correct incident context.
    """
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    # Ignore messages from bots (including ourselves)
    if msg.from_user and msg.from_user.is_bot:
        return

    bot_id = context.bot.id
    bot_username = context.bot.username
    bot_first_name = None
    try:
        bot_info = await context.bot.get_me()
        bot_id = bot_info.id
        bot_username = bot_info.username
        bot_first_name = bot_info.first_name
    except Exception as e:
        logger.warning("Could not fetch bot details: %s", e)

    is_reply = False
    if msg.reply_to_message is not None:
        if msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_id:
            is_reply = True

    is_mention = False
    if bot_username and f"@{bot_username}" in msg.text:
        is_mention = True
    elif bot_first_name and bot_first_name.lower() in msg.text.lower():
        is_mention = True

    if not is_reply and not is_mention:
        return

    dry_run = get_settings().dry_run
    reply_to_id = str(msg.reply_to_message.message_id) if is_reply else None
    chat_id = str(msg.chat_id)
    user_id = str(msg.from_user.id) if getattr(msg, "from_user", None) else f"channel_{msg.chat_id}"
    question = msg.text.strip()

    if not question:
        return

    cleaned_question = clean_mentions(question, bot_username, bot_first_name)

    # Check for commands
    from srebot.bot.commands import handle_command, is_command_message

    command_text = None
    if is_command_message(question):
        command_text = question
    elif is_command_message(cleaned_question):
        command_text = cleaned_question

    if command_text:
        cmd_reply_to_id = reply_to_id or (str(msg.message_id) if is_reply else None)
        response = await handle_command(
            command_text, cmd_reply_to_id, chat_id, bot_username=context.bot.username
        )
        if response:
            await _reply(msg, markdown_to_telegram_html(response), dry_run)
            return

    logger.debug(
        "Follow-up reply/mention from user=%s (reply_to=%s, mention=%s): %.80s",
        user_id,
        reply_to_id,
        is_mention,
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

    user_display_name = None
    if getattr(msg, "from_user", None):
        user = msg.from_user
        if user.username:
            user_display_name = f"@{user.username}"
        else:
            parts = [user.first_name]
            if user.last_name:
                parts.append(user.last_name)
            user_display_name = " ".join(p for p in parts if p)

    if not cleaned_question:
        if indicator:
            try:
                await indicator.delete()
            except Exception as exc:
                logger.warning("Could not delete follow-up indicator: %s", exc)
        return

    answer, new_incident_id, rejection = await handle_followup_question(
        reply_to_id=reply_to_id,
        question=cleaned_question,
        user_id=user_id,
        chat_id=chat_id,
        user_display_name=user_display_name,
    )

    if rejection is not None:
        if rejection == RejectionReason.NO_CONTEXT:
            # Silently ignore
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
    new_bot_msg = None
    if indicator:
        try:
            safe_answer = markdown_to_telegram_html(answer)
            new_bot_msg = await indicator.edit_text(safe_answer, parse_mode=ParseMode.HTML)
        except Exception as exc:
            logger.warning("Could not edit follow-up answer: %s", exc)
            try:
                new_bot_msg = await msg.reply_text(
                    markdown_to_telegram_html(answer),
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=msg.message_id,
                )
            except Exception as exc2:
                logger.error("Total failure sending follow-up answer: %s", exc2)
    else:
        try:
            new_bot_msg = await msg.reply_text(
                markdown_to_telegram_html(answer),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=msg.message_id,
            )
        except Exception as exc:
            logger.error("Failure sending follow-up answer (no indicator): %s", exc)

    final_msg_id = None
    if new_bot_msg and hasattr(new_bot_msg, "message_id"):
        final_msg_id = new_bot_msg.message_id
    elif indicator:
        final_msg_id = indicator.message_id

    if final_msg_id and new_incident_id:
        store = await get_store()
        fingerprint = None
        if reply_to_id:
            fingerprint = await store.get_fp_by_message_id(reply_to_id)
        if not fingerprint and chat_id:
            fingerprint = await store.get_last_active_incident(chat_id)
        if not fingerprint:
            fingerprint = "general_query"

        await store.register_bot_message(final_msg_id, fingerprint, incident_id=new_incident_id)
        await store.set_last_active_incident(chat_id, fingerprint)


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

    # Ignore direct mentions (handled by followup_reply_handler)
    bot_username = context.bot.username
    bot_first_name = None
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        bot_first_name = bot_info.first_name
    except Exception:
        pass

    is_mention = False
    if bot_username and f"@{bot_username}" in msg.text:
        is_mention = True
    elif bot_first_name and bot_first_name.lower() in msg.text.lower():
        is_mention = True

    if is_mention:
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
