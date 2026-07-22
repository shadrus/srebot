"""Telegram bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.shared import (
    ChatAdapter,
    RejectionReason,
    handle_followup_question,
    process_alert_text,
)
from srebot.bot.telegram.html_utils import markdown_to_telegram_html
from srebot.messages import get_chat_message
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)


def get_msg(key: str) -> str:
    lang = config.get_settings().llm_response_language
    return get_chat_message(key, lang, "telegram")


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


class TelegramChatAdapter(ChatAdapter):
    def __init__(self, source_msg: Message, dry_run: bool):
        self.source_msg = source_msg
        self.dry_run = dry_run

    def get_chat_id(self) -> str:
        return f"telegram:{self.source_msg.chat_id}"

    async def send_resolved(
        self, label: str, primary: Alert, current_status: str, reply_to_id: str | None
    ) -> None:
        try:
            await _reply(
                self.source_msg,
                get_msg("resolved_alert").format(
                    alertname=primary.alertname,
                    cluster=primary.cluster,
                    job=primary.labels.get("job", "—"),
                ),
                self.dry_run,
            )
        except Exception as exc:
            logger.warning("Could not send resolved reply: %s", exc)

    async def send_short_notification(
        self, group_fp: str, label: str, primary: Alert
    ) -> str | int | None:
        msg_text = get_msg("new_alert").format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Telegram short notification:\n%s", msg_text)
            return None
        try:
            placeholder = await self.source_msg.reply_text(
                msg_text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=self.source_msg.message_id,
            )
            return placeholder.message_id
        except Exception as exc:
            logger.error("Failed to send short notification: %s", exc)
            return None

    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> str | int | None:
        if self.dry_run:
            logger.info(
                "[DRY-RUN] Analyzing group %s: %d alert(s) (no Telegram placeholder sent)",
                group_fp,
                alert_count,
            )
            return None
        try:
            placeholder = await self.source_msg.reply_text(
                get_msg("analyzing_alerts").format(count=alert_count) + "\n"
                f"<code>{primary.alertname}</code> · "
                f"{primary.cluster} · {primary.labels.get('job', '')}",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=self.source_msg.message_id,
            )
            return placeholder.message_id
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return None

    async def update_with_analysis(
        self, group_fp: str, placeholder_id: str | int | None, analysis: str, is_billing_error: bool
    ) -> str | int | None:
        safe_analysis = markdown_to_telegram_html(analysis)
        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            safe_analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        if placeholder_id is None:
            if not self.dry_run:
                msg = await _reply(self.source_msg, safe_analysis, self.dry_run)
                return msg.message_id if msg else None
            return None

        try:
            await self.source_msg.get_bot().edit_message_text(
                chat_id=self.source_msg.chat_id,
                message_id=placeholder_id,
                text=safe_analysis,
                parse_mode=ParseMode.HTML,
            )
            return placeholder_id
        except Exception as exc:
            logger.error("Could not update placeholder %s with analysis: %s", placeholder_id, exc)
            msg = await _reply(self.source_msg, safe_analysis, self.dry_run)
            return msg.message_id if msg else None


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    source_msg: Message,
) -> None:
    """
    Process a group of related alerts as a single analysis.
    Delegates to shared workflow.
    """
    from srebot.bot.shared import execute_alert_group_workflow

    dry_run = config.get_settings().dry_run
    adapter = TelegramChatAdapter(source_msg, dry_run)
    await execute_alert_group_workflow(group_fp, alerts, adapter, dry_run)


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

    dry_run = config.get_settings().dry_run
    reply_to_id = str(msg.reply_to_message.message_id) if is_reply else None
    chat_id = f"telegram:{msg.chat_id}"
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

    async def report_tool_failure(_failed_tools: list[str]) -> None:
        if indicator:
            await indicator.edit_text(
                get_msg("mcp_failure_progress"),
                parse_mode=ParseMode.HTML,
            )

    answer, new_incident_id, fp_used, rejection = await handle_followup_question(
        reply_to_id=reply_to_id,
        question=cleaned_question,
        user_id=user_id,
        chat_id=chat_id,
        user_display_name=user_display_name,
        on_tool_failure=report_tool_failure,
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
            max_turns = config.get_settings().followup_max_turns
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

    if final_msg_id and new_incident_id and fp_used:
        store = await state_store.get_store()
        await store.register_bot_message(final_msg_id, fp_used, incident_id=new_incident_id)
        await store.set_last_active_incident(chat_id, fp_used)


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
        logger.exception("Could not fetch Telegram bot details while checking mention")

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
            dry_run = config.get_settings().dry_run
            response = await handle_command(
                msg.text, None, chat_id, bot_username=context.bot.username
            )
            if response:
                await _reply(msg, markdown_to_telegram_html(response), dry_run)
                return

    await process_alert_text(msg.text, _handle_alert_group, msg)
