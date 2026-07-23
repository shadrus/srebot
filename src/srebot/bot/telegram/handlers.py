"""Telegram bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import srebot.config as config
from srebot.bot.delivery import (
    DeliveryReceipt,
    MessageConstraints,
    delivery_coordinator,
    html_visible_length,
    paginate_markdown,
)
from srebot.bot.shared import (
    ChatAdapter,
    RejectionReason,
    handle_followup_question,
    process_alert_text,
    register_followup_receipt,
    rejection_turn_limit,
)
from srebot.bot.telegram.html_utils import markdown_to_telegram_html
from srebot.messages import get_chat_message
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 3800
TELEGRAM_CONSTRAINTS = MessageConstraints(
    TELEGRAM_MESSAGE_LIMIT,
    "telegram",
    measure=html_visible_length,
)


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


async def _deliver_markdown(
    source_msg: Message,
    text: str,
    *,
    placeholder_id: str | int | None = None,
    first_message: Message | None = None,
) -> DeliveryReceipt:
    """Deliver ordered Telegram HTML chunks rendered from canonical Markdown."""
    chunks = paginate_markdown(text, TELEGRAM_CONSTRAINTS, markdown_to_telegram_html)

    async def send_chunk(rendered: str, _primary_id: str | None) -> str:
        sent = await source_msg.reply_text(
            rendered,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=source_msg.message_id,
        )
        return str(sent.message_id)

    async def edit_first(rendered: str) -> str:
        if first_message is not None:
            await first_message.edit_text(rendered, parse_mode=ParseMode.HTML)
            return str(first_message.message_id)
        if placeholder_id is None:
            raise RuntimeError("no Telegram placeholder is available")
        await source_msg.get_bot().edit_message_text(
            chat_id=source_msg.chat_id,
            message_id=placeholder_id,
            text=rendered,
            parse_mode=ParseMode.HTML,
        )
        return str(placeholder_id)

    return await delivery_coordinator.deliver(
        f"telegram:{source_msg.chat_id}",
        chunks,
        send_chunk,
        edit_first if placeholder_id is not None or first_message is not None else None,
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
        text = get_chat_message(
            "resolved_alert",
            config.get_settings().llm_response_language,
            "markdown",
        ).format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        try:
            if self.dry_run:
                logger.info("[DRY-RUN] Would send Telegram resolved message:\n%s", text)
            else:
                await _deliver_markdown(self.source_msg, text)
        except Exception as exc:
            logger.warning("Could not send resolved reply: %s", exc)

    async def send_short_notification(
        self, group_fp: str, label: str, primary: Alert
    ) -> DeliveryReceipt:
        msg_text = get_chat_message(
            "new_alert",
            config.get_settings().llm_response_language,
            "markdown",
        ).format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        chunks = paginate_markdown(msg_text, TELEGRAM_CONSTRAINTS, markdown_to_telegram_html)
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Telegram short notification:\n%s", msg_text)
            return DeliveryReceipt.from_ids((), len(chunks))
        try:
            return await _deliver_markdown(self.source_msg, msg_text)
        except Exception as exc:
            logger.error("Failed to send short notification: %s", exc)
            return DeliveryReceipt.from_ids((), len(chunks))

    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> DeliveryReceipt:
        text = (
            get_chat_message(
                "analyzing_alerts",
                config.get_settings().llm_response_language,
                "markdown",
            ).format(count=alert_count)
            + "\n"
            f"`{primary.alertname}` · "
            f"{primary.cluster} · {primary.labels.get('job', '')}"
        )
        chunks = paginate_markdown(text, TELEGRAM_CONSTRAINTS, markdown_to_telegram_html)
        if self.dry_run:
            logger.info(
                "[DRY-RUN] Analyzing group %s: %d alert(s) (no Telegram placeholder sent)",
                group_fp,
                alert_count,
            )
            return DeliveryReceipt.from_ids((), len(chunks))
        try:
            return await _deliver_markdown(self.source_msg, text)
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return DeliveryReceipt.from_ids((), len(chunks))

    async def update_with_analysis(
        self, group_fp: str, placeholder_id: str | int | None, analysis: str, is_billing_error: bool
    ) -> DeliveryReceipt:
        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        chunks = paginate_markdown(analysis, TELEGRAM_CONSTRAINTS, markdown_to_telegram_html)
        if self.dry_run:
            return DeliveryReceipt.from_ids((), len(chunks))
        return await _deliver_markdown(
            self.source_msg,
            analysis,
            placeholder_id=placeholder_id,
        )


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
            if dry_run:
                logger.info("[DRY-RUN] Telegram command response:\n%s", response)
            else:
                await _deliver_markdown(msg, response)
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
            max_turns = rejection_turn_limit(config.get_settings(), rejection)
            user_msg = get_msg("limit_reached").format(current=max_turns, max=max_turns)

        if not dry_run:
            receipt = await _deliver_markdown(msg, user_msg, first_message=indicator)
            await register_followup_receipt(
                receipt,
                fp_used,
                new_incident_id,
                chat_id,
            )
        else:
            logger.info("[DRY-RUN] Follow-up rejection (%s): %s", rejection.value, user_msg)
        return

    if dry_run:
        logger.info("[DRY-RUN] Follow-up answer for user=%s:\n%s", user_id, answer)
        return

    receipt = await _deliver_markdown(msg, answer, first_message=indicator)

    await register_followup_receipt(
        receipt,
        fp_used,
        new_incident_id,
        chat_id,
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
                if dry_run:
                    logger.info("[DRY-RUN] Telegram command response:\n%s", response)
                else:
                    await _deliver_markdown(msg, response)
                return

    await process_alert_text(msg.text, _handle_alert_group, msg)
