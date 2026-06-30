"""Discord bot integration handlers — processes channel messages and orchestrates analysis."""

import logging

import discord
from discord.ext import commands

from srebot.bot.shared import process_alert_text
from srebot.config import Settings, get_settings
from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, AlertStatus
from srebot.state.store import get_store

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
    lang = get_settings().llm_response_language
    return MESSAGES.get(lang, MESSAGES["English"]).get(key, "")


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    message: discord.Message,
) -> None:
    """
    Process a group of related alerts as a single analysis.
    All alerts share the same alertname + cluster + job.
    """
    settings = get_settings()
    dry_run = settings.dry_run
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
            text = (
                f"✅ **Resolved:** `{primary.alertname}`\n"
                f"**Cluster:** {primary.cluster} | "
                f"**Job:** {primary.labels.get('job', '—')}"
            )
            if dry_run:
                logger.info("[DRY-RUN] Would send Discord message:\n%s", text)
            else:
                try:
                    # Reply to the original message if possible
                    await message.reply(text)
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
            placeholder = await message.reply(
                get_msg("analyzing_alerts").format(count=len(alerts)) + "\n"
                f"`{primary.alertname}` · {primary.cluster} · {primary.labels.get('job', '')}"
            )
            # Mark as ANALYZING so concurrent RESOLVED messages know we are on it
            await store.mark_analyzing(group_fp, str(placeholder.id))
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return
    else:
        logger.info(
            "[DRY-RUN] Analyzing group %s: %d alert(s) (no Discord placeholder sent)",
            group_fp,
            len(alerts),
        )
        await store.mark_analyzing(group_fp, "0")

    # Run LLM analysis
    try:
        analysis, incident_id = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ **Analysis failed:** `{exc}`\nPlease investigate manually."
        incident_id = None

    # Append TTL footer if not a billing error
    is_billing_error = (
        "insufficient balance" in analysis.lower() or "недостаточно баланса" in analysis.lower()
    )
    if not is_billing_error:
        ttl_hours = get_settings().followup_ttl // 3600
        analysis += get_msg("ttl_footer").format(hours=ttl_hours)

    # Check if the alert was resolved while we were analyzing
    current_status = await store.get_status(group_fp)

    if dry_run:
        logger.info("[DRY-RUN] Analysis result for group %s:\n%s", group_fp, analysis)
        if current_status == "analyzing":
            await store.mark_firing(group_fp, reply_message_id="0")
        return

    # ALWAYS update the UI with findings, even if already resolved
    try:
        # Discord message limit is 2000 chars, truncate if necessary
        if len(analysis) > 1900:
            analysis = analysis[:1900] + "... (truncated)"

        await placeholder.edit(content=analysis)
    except Exception as exc:
        logger.warning("Could not edit placeholder (%s), sending new message", exc)
        try:
            new_msg = await message.reply(analysis)
            placeholder = new_msg
        except Exception as exc2:
            logger.error("Total failure sending analysis reply: %s", exc2)
            return

    # Only restore FIRING state if it wasn't cleared by a RESOLVED message
    if current_status == "analyzing":
        await store.mark_firing(group_fp, str(placeholder.id))

        # Save follow-up context
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
        await store.register_bot_message(str(placeholder.id), group_fp, incident_id=incident_id)
        await store.set_last_active_incident(f"discord:{message.channel.id}", group_fp)
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


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

        # Check for commands using cleaned text
        from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

        if is_command_message(cleaned_text):
            chat_id = extract_chat_id(message)
            if chat_id:
                reply_to_id = (
                    str(message.reference.message_id)
                    if message.reference and message.reference.message_id
                    else None
                )
                response = await handle_command(cleaned_text, reply_to_id, chat_id)
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

            answer, new_incident_id, rejection = await handle_followup_question(
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

                        if final_msg and new_incident_id:
                            store = await get_store()
                            fingerprint = None
                            if reply_to_id:
                                fingerprint = await store.get_fp_by_message_id(reply_to_id)
                            if not fingerprint and chat_id:
                                fingerprint = await store.get_last_active_incident(chat_id)
                            if not fingerprint:
                                fingerprint = "general_query"

                            await store.register_bot_message(
                                str(final_msg.id), fingerprint, incident_id=new_incident_id
                            )
                            await store.set_last_active_incident(chat_id, fingerprint)
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
