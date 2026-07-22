"""Discord bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

import discord
from discord.ext import commands

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.shared import ChatAdapter, process_alert_text
from srebot.config import Settings
from srebot.messages import get_chat_message
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 1900


def get_msg(key: str) -> str:
    lang = config.get_settings().llm_response_language
    return get_chat_message(key, lang, "discord")


def split_discord_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks without discarding content."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_reply_chunks(
    message: discord.Message,
    text: str,
    first_message: discord.Message | None = None,
) -> list[discord.Message]:
    """Send all Discord chunks, optionally replacing an existing placeholder first."""
    chunks = split_discord_message(text)
    if not chunks:
        return []

    sent: list[discord.Message] = []
    if first_message is not None:
        await first_message.edit(content=chunks[0])
        sent.append(first_message)
    else:
        sent.append(await message.reply(chunks[0]))

    for chunk in chunks[1:]:
        sent.append(await message.reply(chunk))
    return sent


async def _reply_targets_bot(message: discord.Message, bot: commands.Bot) -> bool:
    """Return whether a Discord reply targets this bot or a registered bot message."""
    reference = message.reference
    if reference is None or reference.message_id is None:
        return False

    message_id = str(reference.message_id)
    store = await state_store.get_store()
    if await store.get_bot_message_context(message_id):
        return True

    referenced = getattr(reference, "resolved", None)
    referenced_author = getattr(referenced, "author", None)
    if getattr(referenced_author, "id", None) == getattr(bot.user, "id", None):
        return True

    try:
        referenced = await message.channel.fetch_message(reference.message_id)
    except Exception as exc:
        logger.debug("Could not inspect Discord reply target %s: %s", message_id, exc)
        return False
    return getattr(referenced.author, "id", None) == getattr(bot.user, "id", None)


class DiscordChatAdapter(ChatAdapter):
    def __init__(self, message: discord.Message, dry_run: bool):
        self.message = message
        self.dry_run = dry_run
        self.additional_message_ids: list[str] = []

    def get_chat_id(self) -> str:
        return f"discord:{self.message.channel.id}"

    async def send_resolved(
        self, label: str, primary: Alert, current_status: str, reply_to_id: str | None
    ) -> None:
        text = get_msg("resolved_alert").format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
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
        msg_text = get_msg("new_alert").format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
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
                    sent = await _send_reply_chunks(self.message, analysis)
                    self.additional_message_ids = [str(msg.id) for msg in sent[1:]]
                    return str(sent[0].id) if sent else None
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

        if placeholder:
            try:
                sent = await _send_reply_chunks(self.message, analysis, placeholder)
                self.additional_message_ids = [str(msg.id) for msg in sent[1:]]
                return str(sent[0].id) if sent else None
            except Exception as exc:
                logger.warning("Could not edit placeholder (%s), sending new message", exc)

        # Fallback to replying
        try:
            sent = await _send_reply_chunks(self.message, analysis)
            self.additional_message_ids = [str(msg.id) for msg in sent[1:]]
            return str(sent[0].id) if sent else None
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

    if adapter.additional_message_ids and not dry_run:
        store = await state_store.get_store()
        context = await store.get_followup_context(group_fp)
        incident_id = context.get("incident_id") if context else None
        for message_id in adapter.additional_message_ids:
            await store.register_bot_message(message_id, group_fp, incident_id=incident_id)


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

        has_reply = message.reference is not None and message.reference.message_id is not None
        is_bot_reply = await _reply_targets_bot(message, bot) if has_reply else False
        is_mention = bot.user in message.mentions or (
            bot.user.name.lower() in message.content.lower()
        )
        is_bot_authored = getattr(message.author, "bot", False) is True
        cleaned_text = clean_mentions(message.content, bot.user.id, bot.user.name)

        # Check for commands using raw content first (to support e.g. /mute@srebot)
        from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

        command_text = None
        if not is_bot_authored:
            if is_command_message(message.content):
                command_text = message.content
            elif is_command_message(cleaned_text):
                command_text = cleaned_text

        if command_text and (not has_reply or is_bot_reply):
            chat_id = extract_chat_id(message)
            if chat_id:
                reply_to_id = str(message.reference.message_id) if is_bot_reply else None
                response = await handle_command(
                    command_text, reply_to_id, chat_id, bot_username=bot.user.name
                )
                if response:
                    if not settings.dry_run:
                        try:
                            await _send_reply_chunks(message, response)
                        except Exception as exc:
                            logger.warning("Could not send Discord command response: %s", exc)
                    else:
                        logger.info("[DRY-RUN] Discord command response:\n%s", response)
                    return

        logger.debug("Received message %d from channel_id=%d", message.id, message.channel.id)

        # --- Follow-up detection: replies or direct mentions ---
        if not is_bot_authored and (is_bot_reply or is_mention):
            from srebot.bot.shared import RejectionReason, handle_followup_question

            reply_to_id = str(message.reference.message_id) if is_bot_reply else None
            user_id = str(message.author.id)
            chat_id = f"discord:{message.channel.id}"
            user_display_name = message.author.display_name or message.author.name

            indicator = None
            if not settings.dry_run:
                try:
                    indicator = await message.reply(get_msg("analyzing_followup"))
                except Exception as exc:
                    logger.warning("Could not send Discord follow-up typing indicator: %s", exc)

            if not cleaned_text:
                if indicator and not settings.dry_run:
                    try:
                        await indicator.delete()
                    except Exception as exc:
                        logger.warning("Could not delete Discord follow-up indicator: %s", exc)
                return

            async def report_tool_failure(_failed_tools: list[str]) -> None:
                if indicator and not settings.dry_run:
                    await indicator.edit(content=get_msg("mcp_failure_progress"))

            answer, new_incident_id, fp_used, rejection = await handle_followup_question(
                reply_to_id=reply_to_id,
                question=cleaned_text,
                user_id=user_id,
                chat_id=chat_id,
                user_display_name=user_display_name,
                on_tool_failure=report_tool_failure,
            )

            if rejection is None:
                # Successful follow-up
                if not settings.dry_run:
                    try:
                        sent = await _send_reply_chunks(message, answer, indicator)

                        if sent and new_incident_id and fp_used:
                            store = await state_store.get_store()
                            for sent_message in sent:
                                await store.register_bot_message(
                                    str(sent_message.id), fp_used, incident_id=new_incident_id
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
                            await _send_reply_chunks(message, user_msg)
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
                # RejectionReason.NO_CONTEXT: match Telegram by silently ignoring.
                if indicator and not settings.dry_run:
                    try:
                        await indicator.delete()
                    except Exception as exc:
                        logger.warning("Could not delete Discord follow-up indicator: %s", exc)
                return

        if has_reply:
            logger.debug("Ignoring reply to a non-bot Discord message: %s", message.id)
            return

        # Fall through to normal alert parsing
        await process_alert_text(message.content, _handle_alert_group, message)
