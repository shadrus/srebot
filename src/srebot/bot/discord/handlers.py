"""Discord bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

import discord
from discord.ext import commands

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.shared import ChatAdapter, process_alert_text
from srebot.config import Settings
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)


MESSAGES = {
    "Russian": {
        "analyzing_alerts": "🔍 *Анализирую {count} алерт(ов)…*",
        "ttl_footer": "\n\n*💬 Задайте уточняющие вопросы ответом на это сообщение в течение {hours} ч.*",  # noqa: E501
        "analyzing_followup": "*🔍 Анализирую...*",
        "cooldown": "*⏳ Подождите немного перед следующим вопросом.*",
        "limit_reached": "*🔒 Лимит уточняющих вопросов по этому инциденту исчерпан ({current}/{max}).*",  # noqa: E501
    },
    "English": {
        "analyzing_alerts": "🔍 *Analyzing {count} alert(s)…*",
        "ttl_footer": "\n\n*💬 Ask follow-up questions by replying to this message within {hours} h.*",  # noqa: E501
        "analyzing_followup": "*🔍 Analyzing...*",
        "cooldown": "*⏳ Please wait a bit before the next question.*",
        "limit_reached": "*🔒 Limit of follow-up questions for this incident reached ({current}/{max}).*",  # noqa: E501
    },
}


def get_msg(key: str) -> str:
    lang = config.get_settings().llm_response_language
    return MESSAGES.get(lang, MESSAGES["English"]).get(key, "")


class DiscordChatAdapter(ChatAdapter):
    def __init__(self, message: discord.Message, dry_run: bool):
        self.message = message
        self.dry_run = dry_run

    def get_chat_id(self) -> str:
        return f"discord:{self.message.channel.id}"

    async def send_resolved(
        self, label: str, primary: Alert, current_status: str, reply_to_id: str | None
    ) -> None:
        text = (
            f"✅ **Resolved:** `{primary.alertname}`\n"
            f"**Cluster:** {primary.cluster} | "
            f"**Job:** {primary.labels.get('job', '—')}"
        )
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Discord message:\n%s", text)
        else:
            try:
                await self.message.reply(text)
            except Exception as exc:
                logger.warning("Could not send resolved reply: %s", exc)

    async def send_short_notification(
        self, group_fp: str, label: str, primary: Alert
    ) -> str | int | None:
        msg_text = (
            f"🚨 **New Alert:** `{primary.alertname}`\n"
            f"**Cluster:** {primary.cluster} | "
            f"**Job:** {primary.labels.get('job', '—')}\n\n"
            f"*💬 Reply to this message to run AI analysis.*"
        )
        if self.dry_run:
            logger.info("[DRY-RUN] Short notification: %s", msg_text)
            return None

        try:
            placeholder = await self.message.reply(msg_text)
            return str(placeholder.id)
        except Exception as exc:
            logger.error("Failed to send short notification: %s", exc)
            return None

    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> str | int | None:
        if self.dry_run:
            logger.info(
                "[DRY-RUN] Analyzing group %s: %d alert(s) (no Discord placeholder sent)",
                group_fp,
                alert_count,
            )
            return None

        try:
            placeholder = await self.message.reply(
                get_msg("analyzing_alerts").format(count=alert_count) + "\n"
                f"`{primary.alertname}` · {primary.cluster} · {primary.labels.get('job', '')}"
            )
            return str(placeholder.id)
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return None

    async def update_with_analysis(
        self, group_fp: str, placeholder_id: str | int | None, analysis: str, is_billing_error: bool
    ) -> str | int | None:
        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        if placeholder_id is None:
            if not self.dry_run:
                try:
                    # Discord message limit is 2000 chars, truncate if necessary
                    if len(analysis) > 1900:
                        analysis = analysis[:1900] + "... (truncated)"
                    new_msg = await self.message.reply(analysis)
                    return str(new_msg.id)
                except Exception as exc2:
                    logger.error("Could not send analysis reply: %s", exc2)
                    return None
            return None

        # Try to fetch the original placeholder message by id in order to edit it
        try:
            channel = self.message.channel
            placeholder = await channel.fetch_message(int(placeholder_id))
        except Exception as exc:
            logger.warning("Could not fetch placeholder %s for edit: %s", placeholder_id, exc)
            placeholder = None

        if len(analysis) > 1900:
            analysis = analysis[:1900] + "... (truncated)"

        if placeholder:
            try:
                await placeholder.edit(content=analysis)
                return placeholder_id
            except Exception as exc:
                logger.warning("Could not edit placeholder (%s), sending new message", exc)

        # Fallback to replying
        try:
            new_msg = await self.message.reply(analysis)
            return str(new_msg.id)
        except Exception as exc2:
            logger.error("Total failure sending analysis reply: %s", exc2)
            return None


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    message: discord.Message,
) -> None:
    """
    Process a group of related alerts as a single analysis.
    Delegates to shared workflow.
    """
    from srebot.bot.shared import execute_alert_group_workflow

    dry_run = config.get_settings().dry_run
    adapter = DiscordChatAdapter(message, dry_run)
    await execute_alert_group_workflow(group_fp, alerts, adapter, dry_run)


def clean_mentions(text: str, bot_id: int | None, bot_name: str | None) -> str:
    """Remove bot mentions and name from the message."""
    import re

    cleaned = text
    if bot_id:
        # Match <@ID> or <@!ID>
        pattern = re.compile(rf"(?i)<@!?{bot_id}>")
        cleaned = pattern.sub("", cleaned)
    if bot_name:
        pattern = re.compile(rf"(?i)\b{re.escape(bot_name)}\b")
        cleaned = pattern.sub("", cleaned)
    # Strip any leading/trailing commas, colons, semicolons, and spaces
    cleaned = re.sub(r"^[,\s:;?]+|[,\s:;?]+$", "", cleaned).strip()
    return cleaned


def register_handlers(bot: commands.Bot, settings: Settings) -> None:
    """Register Discord event handlers on the given bot instance."""

    @bot.event
    async def on_message(message: discord.Message) -> None:
        """Handler for all messages."""
        # Ignore our own messages
        if message.author == bot.user:
            return

        # Filter by configured channel
        if message.channel.id != settings.discord_channel_id:
            return

        if not message.content:
            return

        is_reply = message.reference is not None and message.reference.message_id is not None
        is_mention = bot.user in message.mentions or (
            bot.user.name.lower() in message.content.lower()
        )
        cleaned_text = clean_mentions(message.content, bot.user.id, bot.user.name)

        # Check for commands using raw content first (to support e.g. /mute@srebot)
        from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

        command_text = None
        if is_command_message(message.content):
            command_text = message.content
        elif is_command_message(cleaned_text):
            command_text = cleaned_text

        if command_text:
            chat_id = extract_chat_id(message)
            if chat_id:
                reply_to_id = (
                    str(message.reference.message_id)
                    if message.reference and message.reference.message_id
                    else None
                )
                response = await handle_command(
                    command_text, reply_to_id, chat_id, bot_username=bot.user.name
                )
                if response:
                    if not settings.dry_run:
                        try:
                            await message.reply(
                                response[:1900] if len(response) > 1900 else response
                            )
                        except Exception as exc:
                            logger.warning("Could not send Discord command response: %s", exc)
                    else:
                        logger.info("[DRY-RUN] Discord command response:\n%s", response)
                    return

        logger.debug("Received message %d from channel_id=%d", message.id, message.channel.id)

        # --- Follow-up detection: replies or direct mentions ---
        if is_reply or is_mention:
            from srebot.bot.shared import RejectionReason, handle_followup_question

            reply_to_id = str(message.reference.message_id) if is_reply else None
            user_id = str(message.author.id)
            chat_id = f"discord:{message.channel.id}"
            user_display_name = message.author.display_name or message.author.name

            indicator = None
            if not settings.dry_run:
                try:
                    indicator = await message.reply(get_msg("analyzing_followup"))
                except Exception as exc:
                    logger.warning("Could not send Discord follow-up typing indicator: %s", exc)

            answer, new_incident_id, fp_used, rejection = await handle_followup_question(
                reply_to_id=reply_to_id,
                question=cleaned_text,
                user_id=user_id,
                chat_id=chat_id,
                user_display_name=user_display_name,
            )

            if rejection is None:
                # Successful follow-up
                if not settings.dry_run:
                    try:
                        safe_answer = answer[:1900] if len(answer) > 1900 else answer
                        final_msg = indicator
                        if indicator:
                            await indicator.edit(content=safe_answer)
                        else:
                            final_msg = await message.reply(safe_answer)

                        if final_msg and new_incident_id and fp_used:
                            store = await state_store.get_store()
                            await store.register_bot_message(
                                str(final_msg.id), fp_used, incident_id=new_incident_id
                            )
                            await store.set_last_active_incident(chat_id, fp_used)
                    except Exception as exc:
                        logger.warning("Could not send Discord follow-up answer: %s", exc)
                else:
                    logger.info("[DRY-RUN] Discord follow-up answer:\n%s", answer)
                return
            elif rejection != RejectionReason.NO_CONTEXT:
                # Cooldown or limit — send user-facing message
                if rejection == RejectionReason.COOLDOWN:
                    user_msg = get_msg("cooldown")
                else:
                    max_turns = settings.followup_max_turns
                    user_msg = get_msg("limit_reached").format(current=max_turns, max=max_turns)

                if not settings.dry_run:
                    try:
                        if indicator:
                            await indicator.edit(content=user_msg)
                        else:
                            await message.reply(user_msg)
                    except Exception as exc:
                        logger.warning("Could not send Discord follow-up rejection: %s", exc)
                else:
                    logger.info(
                        "[DRY-RUN] Discord follow-up rejection (%s): %s",
                        rejection.value,
                        user_msg,
                    )
                return
            else:
                # RejectionReason.NO_CONTEXT: delete indicator and fall through to alert parsing
                if indicator and not settings.dry_run:
                    try:
                        await indicator.delete()
                    except Exception as exc:
                        logger.warning("Could not delete Discord follow-up indicator: %s", exc)

        # Fall through to normal alert parsing
        await process_alert_text(message.content, _handle_alert_group, message)
