"""Slack bot integration handlers — processes channel messages and orchestrates analysis."""

import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.delivery import (
    DeliveryReceipt,
    MessageConstraints,
    canonicalize_fence_indentation,
    delivery_coordinator,
    paginate_markdown,
)
from srebot.bot.shared import (
    ChatAdapter,
    process_alert_text,
    register_followup_receipt,
    rejection_turn_limit,
)
from srebot.config import Settings
from srebot.messages import get_chat_message
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)

SLACK_MESSAGE_LIMIT = 3800
SLACK_CONSTRAINTS = MessageConstraints(SLACK_MESSAGE_LIMIT, "slack")
_SLACK_FENCE_OPEN_RE = re.compile(r"^(?P<marker>`{3,}|~{3,})[^\r\n]*$")
_SLACK_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_SLACK_INVISIBLE_SEPARATOR = "\u2060"


def get_msg(key: str) -> str:
    lang = config.get_settings().llm_response_language
    return get_chat_message(key, lang, "slack")


def _markdown_to_slack(text: str) -> str:
    """
    Convert standard Markdown to Slack mrkdwn format.
    """
    text = canonicalize_fence_indentation(text)
    code_segments: list[str] = []

    def protect_code(code: str) -> str:
        code_segments.append(code)
        return f"\x00CODE{len(code_segments) - 1}\x00"

    def neutralize_backtick_runs(value: str) -> str:
        def split_run(match: re.Match) -> str:
            run = match.group(0)
            return _SLACK_INVISIBLE_SEPARATOR.join(
                run[index : index + 2] for index in range(0, len(run), 2)
            )

        return re.sub(r"`{3,}", split_run, value)

    lines = text.splitlines(keepends=True)
    protected: list[str] = []
    index = 0
    while index < len(lines):
        opening_line = lines[index].rstrip("\r\n")
        opening_match = _SLACK_FENCE_OPEN_RE.fullmatch(opening_line)
        if opening_match is None:
            protected.append(lines[index])
            index += 1
            continue

        marker = opening_match.group("marker")
        closing_index = index + 1
        while closing_index < len(lines):
            closing_line = lines[closing_index].rstrip("\r\n")
            if re.fullmatch(
                rf"{re.escape(marker[0])}{{{len(marker)},}}[ \t]*",
                closing_line,
            ):
                break
            closing_index += 1
        if closing_index == len(lines):
            protected.append(lines[index])
            index += 1
            continue

        body = "".join(lines[index + 1 : closing_index])
        if body and not body.endswith(("\n", "\r")):
            body = f"{body}\n"
        safe_body = neutralize_backtick_runs(body)
        safe_body = safe_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        protected.append(protect_code(f"```\n{safe_body}```"))
        index = closing_index + 1

    text = "".join(protected)
    text = _SLACK_INLINE_CODE_RE.sub(lambda match: protect_code(match.group(0)), text)
    # Convert italics before bold so the resulting Slack bold markers are not
    # interpreted as Markdown italics by the next substitution.
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"_\1_", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    # Headers: # Header -> *Header*
    text = re.sub(r"^#+\s+(.*?)$", r"*\1*", text, flags=re.MULTILINE)
    # Strikethrough: ~~text~~ -> ~text~
    text = re.sub(r"~~(.*?)~~", r"~\1~", text)
    # Links: [text](url) -> <url|text>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"<\2|\1>", text)

    for index, code in enumerate(code_segments):
        text = text.replace(f"\x00CODE{index}\x00", code)
    return text


async def _deliver_text(
    channel_id: str,
    client: AsyncWebClient,
    text: str,
    *,
    thread_ts: str | None = None,
    placeholder_ts: str | None = None,
) -> DeliveryReceipt:
    """Deliver one ordered Slack mrkdwn response."""
    chunks = paginate_markdown(text, SLACK_CONSTRAINTS, _markdown_to_slack)

    async def send_chunk(rendered: str, primary_id: str | None) -> str:
        response = await client.chat_postMessage(
            channel=channel_id,
            text=rendered,
            thread_ts=thread_ts or primary_id,
        )
        return str(response["ts"])

    async def edit_first(rendered: str) -> str:
        if placeholder_ts is None:
            raise RuntimeError("no Slack placeholder is available")
        await client.chat_update(
            channel=channel_id,
            ts=placeholder_ts,
            text=rendered,
        )
        return placeholder_ts

    return await delivery_coordinator.deliver(
        f"slack:{channel_id}",
        chunks,
        send_chunk,
        edit_first if placeholder_ts is not None else None,
    )


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
        text = get_chat_message(
            "resolved_alert",
            config.get_settings().llm_response_language,
            "markdown",
        ).format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Slack message:\n%s", text)
        else:
            try:
                await _deliver_text(
                    self.channel_id,
                    self.client,
                    text,
                    thread_ts=reply_to_id,
                )
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
        chunks = paginate_markdown(msg_text, SLACK_CONSTRAINTS, _markdown_to_slack)
        if self.dry_run:
            logger.info("[DRY-RUN] Short notification: %s", msg_text)
            return DeliveryReceipt.from_ids((), len(chunks))

        try:
            return await _deliver_text(self.channel_id, self.client, msg_text)
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
            + f"\n`{primary.alertname}` · {primary.cluster} · "
            f"{primary.labels.get('job', '')}"
        )
        chunks = paginate_markdown(text, SLACK_CONSTRAINTS, _markdown_to_slack)
        if self.dry_run:
            logger.info("[DRY-RUN] Analyzing group %s: %d alert(s)", group_fp, alert_count)
            return DeliveryReceipt.from_ids((), len(chunks))

        try:
            return await _deliver_text(self.channel_id, self.client, text)
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return DeliveryReceipt.from_ids((), len(chunks))

    async def update_with_analysis(
        self, group_fp: str, placeholder_id: str | int | None, analysis: str, is_billing_error: bool
    ) -> DeliveryReceipt:
        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        chunks = paginate_markdown(analysis, SLACK_CONSTRAINTS, _markdown_to_slack)
        if self.dry_run:
            return DeliveryReceipt.from_ids((), len(chunks))
        return await _deliver_text(
            self.channel_id,
            self.client,
            analysis,
            placeholder_ts=str(placeholder_id) if placeholder_id is not None else None,
        )


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
            user_id = auth_response.get("user_id")
            username = auth_response.get("user")
            if not isinstance(user_id, str) or not isinstance(username, str):
                raise ValueError("Slack auth_test response is missing bot user data")
            bot_user_id = user_id
            bot_username = username
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

    async def handle_app_mention_events(event: dict, client: AsyncWebClient) -> None:
        """Handler for bot mentions (@srebot)."""
        await handle_message_events(event, event, client)

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

        thread_has_context = False
        if thread_ts:
            store = await state_store.get_store()
            thread_has_context = bool(await store.get_bot_message_context(thread_ts))

        # Check for commands using raw content first (to support e.g. /mute@srebot)
        from srebot.bot.commands import extract_chat_id, handle_command, is_command_message

        command_text = None
        if not is_bot_authored:
            if is_command_message(text):
                command_text = text
            elif is_command_message(cleaned_text):
                command_text = cleaned_text

        if command_text:
            if thread_ts and not thread_has_context:
                logger.debug("Ignoring command in unrelated Slack thread %s", thread_ts)
                return
            chat_id = extract_chat_id(channel_id)
            if chat_id:
                response = await handle_command(
                    command_text, thread_ts, chat_id, bot_username=bot_name
                )
                if response:
                    if not settings.dry_run:
                        try:
                            await _deliver_text(
                                channel_id,
                                client,
                                response,
                                thread_ts=thread_ts,
                            )
                        except Exception as exc:
                            logger.warning("Could not send Slack command response: %s", exc)
                    else:
                        logger.info("[DRY-RUN] Slack command response:\n%s", response)
                    return

        # --- Follow-up detection: thread replies or direct mentions ---
        if not is_bot_authored and ((thread_ts and thread_has_context) or is_mention):
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

            async def report_tool_failure(_failed_tools: list[str]) -> None:
                if indicator_ts and not settings.dry_run:
                    await client.chat_update(
                        channel=channel_id,
                        ts=indicator_ts,
                        text=get_msg("mcp_failure_progress"),
                    )

            answer, new_incident_id, fp_used, rejection = await handle_followup_question(
                reply_to_id=thread_ts if thread_has_context else None,
                question=cleaned_text,
                user_id=str(user_id),
                chat_id=chat_id,
                user_display_name=user_display_name,
                on_tool_failure=report_tool_failure,
            )

            if rejection is None:
                # Successful follow-up
                if not settings.dry_run:
                    try:
                        receipt = await _deliver_text(
                            channel_id,
                            client,
                            answer,
                            thread_ts=reply_to_ts,
                            placeholder_ts=indicator_ts,
                        )

                        await register_followup_receipt(
                            receipt,
                            fp_used,
                            new_incident_id,
                            chat_id,
                            additional_message_ids=(reply_to_ts,),
                        )
                    except Exception as exc:
                        logger.warning("Could not send Slack follow-up answer: %s", exc)
                else:
                    logger.info("[DRY-RUN] Slack follow-up answer:\n%s", answer)
                return
            elif rejection != RejectionReason.NO_CONTEXT:
                if rejection == RejectionReason.COOLDOWN:
                    user_msg = get_msg("cooldown")
                else:
                    max_turns = rejection_turn_limit(settings, rejection)
                    user_msg = get_msg("limit_reached").format(current=max_turns, max=max_turns)

                if not settings.dry_run:
                    try:
                        receipt = await _deliver_text(
                            channel_id,
                            client,
                            user_msg,
                            thread_ts=reply_to_ts,
                            placeholder_ts=indicator_ts,
                        )
                        await register_followup_receipt(
                            receipt,
                            fp_used,
                            new_incident_id,
                            chat_id,
                            additional_message_ids=(reply_to_ts,),
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

        if thread_ts:
            logger.debug("Ignoring message in unrelated Slack thread %s", thread_ts)
            return

        # Fall through to normal alert parsing
        await _process_incoming_text(text, channel_id, client)

    app.event("app_mention")(handle_app_mention_events)
    app.message()(handle_message_events)
