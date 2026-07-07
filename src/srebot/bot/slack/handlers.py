"""Slack bot integration handlers — processes channel messages and orchestrates analysis."""

import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.shared import ChatAdapter, process_alert_text
from srebot.config import Settings
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)


MESSAGES = {
    "Russian": {
        "analyzing_alerts": "🔍 *Анализирую {count} алерт(ов)…*",
        "ttl_footer": "\n\n_💬 Задайте уточняющие вопросы в треде к этому сообщению в течение {hours} ч._",  # noqa: E501
        "analyzing_followup": "🔍 _Анализирую..._",
        "cooldown": "⏳ _Подождите немного перед следующим вопросом._",
        "limit_reached": "🔒 _Лимит уточняющих вопросов по этому инциденту исчерпан ({current}/{max})._",  # noqa: E501
        "resolved_alert": ("✅ *Решено:* `{alertname}`\n*Кластер:* {cluster} | *Job:* {job}"),
        "new_alert": (
            "🚨 *Новый алерт:* `{alertname}`\n"
            "*Кластер:* {cluster} | *Job:* {job}\n\n"
            "_💬 Ответьте в треде к этому сообщению, чтобы запустить AI-анализ._"
        ),
    },
    "English": {
        "analyzing_alerts": "🔍 *Analyzing {count} alert(s)…*",
        "ttl_footer": "\n\n_💬 Ask follow-up questions by replying in this thread within {hours} h._",  # noqa: E501
        "analyzing_followup": "🔍 _Analyzing..._",
        "cooldown": "⏳ _Please wait a bit before the next question._",
        "limit_reached": "🔒 _Limit of follow-up questions for this incident reached ({current}/{max})._",  # noqa: E501
        "resolved_alert": ("✅ *Resolved:* `{alertname}`\n*Cluster:* {cluster} | *Job:* {job}"),
        "new_alert": (
            "🚨 *New Alert:* `{alertname}`\n"
            "*Cluster:* {cluster} | *Job:* {job}\n\n"
            "_💬 Reply in this thread to run AI analysis._"
        ),
    },
}


def get_msg(key: str) -> str:
    lang = config.get_settings().llm_response_language
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


class SlackChatAdapter(ChatAdapter):
    def __init__(self, channel_id: str, client: AsyncWebClient, dry_run: bool):
        self.channel_id = channel_id
        self.client = client
        self.dry_run = dry_run

    def get_chat_id(self) -> str:
        return f"slack:{self.channel_id}"

    async def send_resolved(
        self, label: str, primary: Alert, current_status: str, reply_to_id: str | None
    ) -> None:
        text = get_msg("resolved_alert").format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Slack message:\n%s", text)
        else:
            try:
                await self.client.chat_postMessage(
                    channel=self.channel_id,
                    text=text,
                    thread_ts=reply_to_id if reply_to_id else None,
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
            logger.info("[DRY-RUN] Short notification: %s", msg_text)
            return None

        try:
            res = await self.client.chat_postMessage(channel=self.channel_id, text=msg_text)
            return res["ts"]
        except Exception as exc:
            logger.error("Failed to send short notification: %s", exc)
            return None

    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> str | int | None:
        if self.dry_run:
            logger.info("[DRY-RUN] Analyzing group %s: %d alert(s)", group_fp, alert_count)
            return None

        try:
            res = await self.client.chat_postMessage(
                channel=self.channel_id,
                text=(
                    get_msg("analyzing_alerts").format(count=alert_count)
                    + f"\n`{primary.alertname}` · {primary.cluster} · "
                    f"{primary.labels.get('job', '')}"
                ),
            )
            return res["ts"]
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return None

    async def update_with_analysis(
        self, group_fp: str, placeholder_id: str | int | None, analysis: str, is_billing_error: bool
    ) -> str | int | None:
        # Convert Markdown to Slack mrkdwn
        analysis = _markdown_to_slack(analysis)

        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        if placeholder_id is None:
            if not self.dry_run:
                try:
                    res = await self.client.chat_postMessage(channel=self.channel_id, text=analysis)
                    return res["ts"]
                except Exception as exc2:
                    logger.error("Could not send analysis reply: %s", exc2)
                    return None
            return None

        try:
            await self.client.chat_update(
                channel=self.channel_id,
                ts=placeholder_id,  # type: ignore[arg-type]
                text=analysis,
            )
            return placeholder_id
        except Exception as exc:
            logger.warning("Could not edit placeholder (%s), sending new message", exc)
            try:
                res = await self.client.chat_postMessage(channel=self.channel_id, text=analysis)
                return res["ts"]
            except Exception as exc2:
                logger.error("Could not send analysis reply: %s", exc2)
                return None


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    channel_id: str,
    client: AsyncWebClient,
) -> None:
    """
    Process a group of related alerts as a single analysis.
    Delegates to shared workflow.
    """
    from srebot.bot.shared import execute_alert_group_workflow

    dry_run = config.get_settings().dry_run
    adapter = SlackChatAdapter(channel_id, client, dry_run)
    await execute_alert_group_workflow(group_fp, alerts, adapter, dry_run)


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
        is_bot_authored = bool(
            message.get("bot_id")
            or event.get("bot_id")
            or message.get("subtype") == "bot_message"
            or (bot_id and user_id == bot_id)
        )
        is_mention = bool(bot_id and f"<@{bot_id}>" in text)
        cleaned_text = clean_mentions(text, bot_id)

        # Check for commands using raw content first (to support e.g. /mute@srebot)
        from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

        command_text = None
        if not is_bot_authored:
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
        if not is_bot_authored and (thread_ts or is_mention):
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

            if not cleaned_text:
                if indicator_ts and not settings.dry_run:
                    try:
                        await client.chat_delete(channel=channel_id, ts=indicator_ts)
                    except Exception as exc:
                        logger.warning("Could not delete Slack follow-up indicator: %s", exc)
                return

            answer, new_incident_id, fp_used, rejection = await handle_followup_question(
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

                        if final_msg_ts and new_incident_id and fp_used:
                            store = await state_store.get_store()
                            await store.register_bot_message(
                                str(final_msg_ts), fp_used, incident_id=new_incident_id
                            )
                            await store.set_last_active_incident(chat_id, fp_used)
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
                # RejectionReason.NO_CONTEXT: match Telegram by silently ignoring.
                if indicator_ts and not settings.dry_run:
                    try:
                        await client.chat_delete(channel=channel_id, ts=indicator_ts)
                    except Exception as exc:
                        logger.warning("Could not delete Slack follow-up indicator: %s", exc)
                return

        # Fall through to normal alert parsing
        await _process_incoming_text(text, channel_id, client)
