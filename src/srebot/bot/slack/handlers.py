"""Slack bot integration handlers — processes channel messages and orchestrates analysis."""

import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from srebot.bot.shared import process_alert_text
from srebot.config import Settings, get_settings
from srebot.llm.agent import get_agent
from srebot.parser.alert_parser import Alert, AlertStatus
from srebot.state.store import get_store

logger = logging.getLogger(__name__)


MESSAGES = {
    "Russian": {
        "analyzing_alerts": "🔍 *Анализирую {count} алерт(ов)…*",
        "ttl_footer": "\n\n_💬 Задайте уточняющие вопросы в треде к этому сообщению в течение {hours} ч._",  # noqa: E501
        "analyzing_followup": "🔍 _Анализирую..._",
        "cooldown": "⏳ _Подождите немного перед следующим вопросом._",
        "limit_reached": "🔒 _Лимит уточняющих вопросов по этому инциденту исчерпан ({current}/{max})._",  # noqa: E501
    },
    "English": {
        "analyzing_alerts": "🔍 *Analyzing {count} alert(s)…*",
        "ttl_footer": "\n\n_💬 Ask follow-up questions by replying in this thread within {hours} h._",  # noqa: E501
        "analyzing_followup": "🔍 _Analyzing..._",
        "cooldown": "⏳ _Please wait a bit before the next question._",
        "limit_reached": "🔒 _Limit of follow-up questions for this incident reached ({current}/{max})._",  # noqa: E501
    },
}


def get_msg(key: str) -> str:
    lang = get_settings().llm_response_language
    return MESSAGES.get(lang, MESSAGES["English"]).get(key, "")


def _markdown_to_slack(text: str) -> str:
    """
    Convert standard Markdown to Slack mrkdwn format.
    """
    # 1. Bold: **text** -> *text*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    # 2. Italic: *text* -> _text_ (only if not already bold)
    # This is tricky because of Slack's non-standard markdown.
    # We'll do a simple swap for now.
    text = re.sub(r"(?<!\*)\*(.*?)\*(?!\*)", r"_\1_", text)
    # 3. Headers: # Header -> *Header*
    text = re.sub(r"^#+\s+(.*?)$", r"*\1*", text, flags=re.MULTILINE)
    # 4. Strikethrough: ~~text~~ -> ~text~
    text = re.sub(r"~~(.*?)~~", r"~\1~", text)
    # 5. Links: [text](url) -> <url|text>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"<\2|\1>", text)

    return text


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    channel_id: str,
    client: AsyncWebClient,
) -> None:
    """
    Process a group of related alerts as a single analysis.

    Args:
        group_fp: Fingerprint identifying this alert group.
        alerts: List of alerts belonging to the group.
        channel_id: Slack channel ID to post messages to.
        client: Slack async web client instance.
    """
    dry_run = get_settings().dry_run
    store = await get_store()
    agent = get_agent()

    primary = alerts[0]
    label = f"{primary.alertname} ({primary.cluster}/{primary.labels.get('job', '')})"

    # --- RESOLVED ---
    if primary.status == AlertStatus.RESOLVED:
        current_status = await store.get_status(group_fp)
        reply_to_ts = await store.get_reply_message_id(group_fp)

        await store.mark_resolved(group_fp)
        logger.info("Resolved group [%s]: %s (was %s)", group_fp, label, current_status)

        if current_status in ("firing", "analyzing") or reply_to_ts:
            text = (
                f"✅ *Resolved:* `{primary.alertname}`\n"
                f"*Cluster:* {primary.cluster} | "
                f"*Job:* {primary.labels.get('job', '—')}"
            )
            if dry_run:
                logger.info("[DRY-RUN] Would send Slack message:\n%s", text)
            else:
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=text,
                        thread_ts=reply_to_ts if reply_to_ts else None,
                    )
                except Exception as exc:
                    logger.warning("Could not send resolved reply: %s", exc)
        return
    # --- FIRING ---
    if not await store.is_new(group_fp):
        logger.info("Duplicate firing group (skip): %s [%s]", label, group_fp)
        return

    logger.info("New firing group [%s]: %s (%d alert(s))", group_fp, label, len(alerts))

    placeholder_ts = None
    if not dry_run:
        try:
            res = await client.chat_postMessage(
                channel=channel_id,
                text=(
                    get_msg("analyzing_alerts").format(count=len(alerts))
                    + f"\n`{primary.alertname}` · {primary.cluster} · "
                    f"{primary.labels.get('job', '')}"
                ),
            )
            placeholder_ts = res["ts"]
            # Mark as ANALYZING so concurrent RESOLVED messages know we are on it
            await store.mark_analyzing(group_fp, placeholder_ts)
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return
    else:
        logger.info("[DRY-RUN] Analyzing group %s: %d alert(s)", group_fp, len(alerts))
        await store.mark_analyzing(group_fp, "0")

    # Run LLM analysis
    try:
        analysis, incident_id = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ *Analysis failed:* `{exc}`\nPlease investigate manually."
        incident_id = None

    # Convert Markdown to Slack mrkdwn
    analysis = _markdown_to_slack(analysis)

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
        await client.chat_update(
            channel=channel_id,
            ts=placeholder_ts,  # type: ignore[arg-type]
            text=analysis,
        )
    except Exception as exc:
        logger.warning("Could not edit placeholder (%s), sending new message", exc)
        try:
            res = await client.chat_postMessage(channel=channel_id, text=analysis)
            placeholder_ts = res["ts"]
        except Exception as exc2:
            logger.error("Could not send analysis reply: %s", exc2)
            return

    # Only restore FIRING state if it wasn't cleared by a RESOLVED message
    if current_status == "analyzing":
        await store.mark_firing(group_fp, placeholder_ts)  # type: ignore[arg-type]

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
        await store.register_bot_message(str(placeholder_ts), group_fp, incident_id=incident_id)
        await store.set_last_active_incident(f"slack:{channel_id}", group_fp)
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


bot_user_id = None
bot_username = None


async def get_bot_info(client: AsyncWebClient) -> tuple[str, str]:
    """Retrieve and cache the bot's user ID and username."""
    global bot_user_id, bot_username
    if bot_user_id is None or bot_username is None:
        try:
            auth_response = await client.auth_test()
            bot_user_id = auth_response["user_id"]
            bot_username = auth_response["user"]
        except Exception as exc:
            logger.warning("Could not fetch Slack bot info: %s", exc)
    return bot_user_id or "", bot_username or ""


async def get_bot_user_id(client: AsyncWebClient) -> str:
    """Retrieve and cache the bot's user ID."""
    uid, _ = await get_bot_info(client)
    return uid


def clean_mentions(text: str, bot_id: str | None) -> str:
    """Remove bot mentions (e.g. <@U12345>) from the message text."""
    cleaned = text
    if bot_id:
        pattern = re.compile(rf"(?i)<@{re.escape(bot_id)}>")
        cleaned = pattern.sub("", cleaned)
    # Strip any leading/trailing commas, colons, semicolons, and spaces
    cleaned = re.sub(r"^[,\s:;?]+|[,\s:;?]+$", "", cleaned).strip()
    return cleaned


def register_handlers(app: AsyncApp, settings: Settings) -> None:
    """Register Slack event handlers on the given Bolt application.

    Args:
        app: The Slack Bolt async application instance.
        settings: Application-wide settings (used to filter by channel ID).
    """

    @app.error
    async def global_error_handler(error: Exception, body: dict, logger=logger) -> None:
        """Centralized Slack framework-level error handler."""
        logger.exception("Unhandled Slack framework error: %s | body=%s", error, body)

    async def _process_incoming_text(text: str, channel_id: str, client: AsyncWebClient) -> None:
        """Internal helper to parse and process alert text from any Slack event."""
        if channel_id != settings.slack_channel_id:
            logger.debug("Ignoring message from unconfigured channel %s", channel_id)
            return

        if not text:
            return

        await process_alert_text(text, _handle_alert_group, channel_id, client)

    @app.event("app_mention")
    async def handle_app_mention_events(event: dict, client: AsyncWebClient) -> None:
        """Handler for bot mentions (@srebot)."""
        channel_id = event.get("channel", "")
        if channel_id == settings.slack_channel_id:
            # Let handle_message_events handle it to avoid duplicate processing
            return
        await _process_incoming_text(event.get("text", ""), channel_id, client)

    @app.message()
    async def handle_message_events(event: dict, message: dict, client: AsyncWebClient) -> None:
        """
        Handler for all channel messages.

        Filters by configured channel, detects follow-up thread replies and direct mentions,
        then falls through to normal alert parsing.
        """
        channel_id = event.get("channel", "")
        text = message.get("text", "")
        thread_ts = event.get("thread_ts")
        user_id = event.get("user", "unknown")

        if channel_id != settings.slack_channel_id:
            logger.debug("Ignoring message from unconfigured channel %s", channel_id)
            return

        if not text:
            return

        bot_id, bot_name = await get_bot_info(client)
        is_mention = bool(bot_id and f"<@{bot_id}>" in text)
        cleaned_text = clean_mentions(text, bot_id)

        # Check for commands using raw content first (to support e.g. /mute@srebot)
        from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

        command_text = None
        if is_command_message(text):
            command_text = text
        elif is_command_message(cleaned_text):
            command_text = cleaned_text

        if command_text:
            chat_id = extract_chat_id(channel_id)
            if chat_id:
                response = await handle_command(
                    command_text, thread_ts, chat_id, bot_username=bot_name
                )
                if response:
                    if not settings.dry_run:
                        try:
                            await client.chat_postMessage(
                                channel=channel_id,
                                text=_markdown_to_slack(response),
                                thread_ts=thread_ts,
                            )
                        except Exception as exc:
                            logger.warning("Could not send Slack command response: %s", exc)
                    else:
                        logger.info("[DRY-RUN] Slack command response:\n%s", response)
                    return

        # --- Follow-up detection: thread replies or direct mentions ---
        if thread_ts or is_mention:
            from srebot.bot.shared import RejectionReason, handle_followup_question

            user_display_name = None
            if not settings.dry_run:
                try:
                    user_info = await client.users_info(user=user_id)
                    user_data = user_info.get("user") or {}
                    profile = user_data.get("profile") or {}
                    user_display_name = (
                        profile.get("display_name")
                        or user_data.get("real_name")
                        or user_data.get("name")
                    )
                except Exception as exc:
                    logger.warning("Could not fetch Slack user info: %s", exc)

            chat_id = f"slack:{channel_id}"
            reply_to_ts = thread_ts or event.get("ts")

            indicator_ts = None
            if not settings.dry_run:
                try:
                    res = await client.chat_postMessage(
                        channel=channel_id,
                        text=get_msg("analyzing_followup"),
                        thread_ts=reply_to_ts,
                    )
                    indicator_ts = res["ts"]
                except Exception as exc:
                    logger.warning("Could not send Slack follow-up typing indicator: %s", exc)

            answer, new_incident_id, rejection = await handle_followup_question(
                reply_to_id=thread_ts,
                question=cleaned_text,
                user_id=str(user_id),
                chat_id=chat_id,
                user_display_name=user_display_name,
            )

            if rejection is None:
                # Successful follow-up
                if not settings.dry_run:
                    try:
                        final_msg_ts = indicator_ts
                        if indicator_ts:
                            await client.chat_update(
                                channel=channel_id,
                                ts=indicator_ts,
                                text=_markdown_to_slack(answer),
                            )
                        else:
                            res = await client.chat_postMessage(
                                channel=channel_id,
                                text=_markdown_to_slack(answer),
                                thread_ts=reply_to_ts,
                            )
                            final_msg_ts = res["ts"]

                        if final_msg_ts and new_incident_id:
                            store = await get_store()
                            fingerprint = None
                            if thread_ts:
                                fingerprint = await store.get_fp_by_message_id(str(thread_ts))
                            if not fingerprint and chat_id:
                                fingerprint = await store.get_last_active_incident(chat_id)
                            if not fingerprint:
                                fingerprint = "general_query"

                            await store.register_bot_message(
                                str(final_msg_ts), fingerprint, incident_id=new_incident_id
                            )
                            await store.set_last_active_incident(chat_id, fingerprint)
                    except Exception as exc:
                        logger.warning("Could not send Slack follow-up answer: %s", exc)
                else:
                    logger.info("[DRY-RUN] Slack follow-up answer:\n%s", answer)
                return
            elif rejection != RejectionReason.NO_CONTEXT:
                if rejection == RejectionReason.COOLDOWN:
                    user_msg = get_msg("cooldown")
                else:
                    max_turns = settings.followup_max_turns
                    user_msg = get_msg("limit_reached").format(current=max_turns, max=max_turns)

                if not settings.dry_run:
                    try:
                        if indicator_ts:
                            await client.chat_update(
                                channel=channel_id,
                                ts=indicator_ts,
                                text=user_msg,
                            )
                        else:
                            await client.chat_postMessage(
                                channel=channel_id,
                                text=user_msg,
                                thread_ts=reply_to_ts,
                            )
                    except Exception as exc:
                        logger.warning("Could not send Slack follow-up rejection: %s", exc)
                else:
                    logger.info(
                        "[DRY-RUN] Slack follow-up rejection (%s): %s",
                        rejection.value,
                        user_msg,
                    )
                return
            else:
                # RejectionReason.NO_CONTEXT: delete indicator and fall through to alert parsing
                if indicator_ts and not settings.dry_run:
                    try:
                        await client.chat_delete(channel=channel_id, ts=indicator_ts)
                    except Exception as exc:
                        logger.warning("Could not delete Slack follow-up indicator: %s", exc)

        # Fall through to normal alert parsing
        await _process_incoming_text(text, channel_id, client)
