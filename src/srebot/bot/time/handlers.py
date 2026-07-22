"""Time Messenger handlers backed by the shared alert and follow-up workflows."""

import logging
import re
from dataclasses import dataclass

from aiotimebot import EventType, HandlerContext, PostedEvent, Propagation, Router, TimeClient
from aiotimebot.api.api.posts import delete_post, patch_post
from aiotimebot.api.models.app_error import AppError
from aiotimebot.api.models.patch_post_body import PatchPostBody

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.messages import get_chat_message
from srebot.bot.shared import (
    ChatAdapter,
    RejectionReason,
    execute_alert_group_workflow,
    handle_followup_question,
    process_alert_text,
)
from srebot.config import Settings
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TimeBotIdentity:
    """Authenticated Time account identity used for self-filtering and mentions."""

    user_id: str
    username: str


def get_msg(key: str) -> str:
    """Return a localized Time Messenger UI string."""
    language = config.get_settings().llm_response_language
    return get_chat_message(key, language, "time")


def clean_mentions(text: str, bot_username: str | None) -> str:
    """Remove Time's ``@username`` mention from an incoming message."""
    cleaned = text
    if bot_username:
        pattern = re.compile(rf"(?i)@{re.escape(bot_username)}\b")
        cleaned = pattern.sub("", cleaned)
    return re.sub(r"^[,\s:;?]+|[,\s:;?]+$", "", cleaned).strip()


def _thread_root(event: PostedEvent) -> str:
    """Return the existing thread root, or make the current post the root."""
    return event.post.root_id or event.post.id


async def _edit_post(client: TimeClient, post_id: str, text: str) -> None:
    """Edit a Time post and normalize generated API error responses."""
    result = await patch_post.asyncio(
        post_id=post_id,
        client=client.api,
        body=PatchPostBody(message=text),
    )
    if isinstance(result, AppError):
        raise RuntimeError(result.message)
    if result is None:
        raise RuntimeError("Time returned an empty response while editing a post")


async def _delete_post(client: TimeClient, post_id: str) -> None:
    """Delete a Time post and normalize generated API error responses."""
    result = await delete_post.asyncio(post_id=post_id, client=client.api)
    if isinstance(result, AppError):
        raise RuntimeError(result.message)
    if result is None:
        raise RuntimeError("Time returned an empty response while deleting a post")


async def _send_thread_message(
    client: TimeClient,
    event: PostedEvent,
    text: str,
    dry_run: bool,
) -> str | None:
    """Reply in the incoming post's Time thread, or log the reply in dry-run mode."""
    if dry_run:
        logger.info("[DRY-RUN] Would send Time message:\n%s", text)
        return None
    post = await client.send_message(
        channel_id=event.post.channel_id,
        root_id=_thread_root(event),
        text=text,
    )
    return post.id


async def _send_channel_message(
    client: TimeClient,
    channel_id: str,
    text: str,
    dry_run: bool,
) -> str | None:
    """Send a top-level Time post that can own an unambiguous incident thread."""
    if dry_run:
        logger.info("[DRY-RUN] Would send Time message:\n%s", text)
        return None
    post = await client.send_message(channel_id=channel_id, text=text)
    return post.id


class TimeChatAdapter(ChatAdapter):
    """Map the platform-neutral alert workflow to Time posts and threads."""

    def __init__(self, event: PostedEvent, client: TimeClient, dry_run: bool) -> None:
        self.event = event
        self.client = client
        self.dry_run = dry_run

    def get_chat_id(self) -> str:
        """Return the namespaced Time channel identifier."""
        return f"time:{self.event.post.channel_id}"

    async def send_resolved(
        self, label: str, primary: Alert, current_status: str, reply_to_id: str | None
    ) -> None:
        """Post a resolved notification in the original analysis thread."""
        text = get_msg("resolved_alert").format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        try:
            if reply_to_id and not self.dry_run:
                await self.client.send_message(
                    channel_id=self.event.post.channel_id,
                    root_id=reply_to_id,
                    text=text,
                )
            else:
                await _send_channel_message(
                    self.client, self.event.post.channel_id, text, self.dry_run
                )
        except Exception as exc:
            logger.warning("Could not send Time resolved reply: %s", exc)

    async def send_short_notification(
        self, group_fp: str, label: str, primary: Alert
    ) -> str | int | None:
        """Post the non-automatic-analysis notification."""
        text = get_msg("new_alert").format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        try:
            return await _send_channel_message(
                self.client, self.event.post.channel_id, text, self.dry_run
            )
        except Exception as exc:
            logger.error("Failed to send Time short notification: %s", exc)
            return None

    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> str | int | None:
        """Post an analysis placeholder in the incoming alert thread."""
        text = (
            get_msg("analyzing_alerts").format(count=alert_count)
            + f"\n`{primary.alertname}` · {primary.cluster} · {primary.labels.get('job', '')}"
        )
        try:
            return await _send_channel_message(
                self.client, self.event.post.channel_id, text, self.dry_run
            )
        except Exception as exc:
            logger.error("Failed to send Time placeholder: %s", exc)
            return None

    async def update_with_analysis(
        self,
        group_fp: str,
        placeholder_id: str | int | None,
        analysis: str,
        is_billing_error: bool,
    ) -> str | int | None:
        """Replace the placeholder with the analysis, falling back to a new reply."""
        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        if placeholder_id is not None and not self.dry_run:
            try:
                await _edit_post(self.client, str(placeholder_id), analysis)
                return placeholder_id
            except Exception as exc:
                logger.warning("Could not edit Time placeholder %s: %s", placeholder_id, exc)

        try:
            return await _send_channel_message(
                self.client, self.event.post.channel_id, analysis, self.dry_run
            )
        except Exception as exc:
            logger.error("Could not send Time analysis reply: %s", exc)
            return None


async def _handle_alert_group(
    group_fp: str,
    alerts: list[Alert],
    event: PostedEvent,
    client: TimeClient,
) -> None:
    """Run the shared alert workflow for one Time alert group."""
    dry_run = config.get_settings().dry_run
    adapter = TimeChatAdapter(event, client, dry_run)
    await execute_alert_group_workflow(group_fp, alerts, adapter, dry_run)


async def _get_user_display_name(client: TimeClient, user_id: str) -> str | None:
    """Fetch a useful Time display name for follow-up attribution."""
    try:
        payload = await client.raw_request("GET", f"/api/v4/users/{user_id}")
    except Exception as exc:
        logger.debug("Could not fetch Time user %s: %s", user_id, exc)
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("username")
    if username:
        return f"@{username}"
    full_name = " ".join(
        str(value).strip()
        for value in (payload.get("first_name"), payload.get("last_name"))
        if value
    )
    return full_name or None


async def _finish_followup(
    client: TimeClient,
    event: PostedEvent,
    indicator_id: str | None,
    text: str,
    dry_run: bool,
) -> str | None:
    """Edit the follow-up indicator or send a fresh thread reply."""
    if dry_run:
        logger.info("[DRY-RUN] Time follow-up response:\n%s", text)
        return None
    if indicator_id:
        try:
            await _edit_post(client, indicator_id, text)
            return indicator_id
        except Exception as exc:
            logger.warning("Could not edit Time follow-up indicator %s: %s", indicator_id, exc)
    return await _send_thread_message(client, event, text, dry_run=False)


async def handle_posted_event(
    event: PostedEvent,
    client: TimeClient,
    settings: Settings,
    identity: TimeBotIdentity,
) -> Propagation:
    """Handle commands, follow-ups, and alerts from one Time post."""
    post = event.post
    if post.channel_id != settings.time_channel_id or not post.message:
        return Propagation.STOP
    if post.user_id == identity.user_id:
        return Propagation.STOP

    text = post.message
    root_id = post.root_id or None
    store = await state_store.get_store()
    thread_has_context = bool(root_id and await store.get_bot_message_context(root_id))
    mention_pattern = (
        re.compile(rf"(?i)(?<!\w)@{re.escape(identity.username)}\b") if identity.username else None
    )
    is_mention = bool(mention_pattern and mention_pattern.search(text))
    cleaned_text = clean_mentions(text, identity.username)

    from srebot.bot.commands import handle_command, is_command_message

    command_text = text if is_command_message(text) else None
    if command_text is None and is_command_message(cleaned_text):
        command_text = cleaned_text

    if command_text and (not root_id or thread_has_context or is_mention):
        response = await handle_command(
            command_text,
            root_id if thread_has_context else None,
            f"time:{post.channel_id}",
            bot_username=identity.username,
        )
        if response:
            await _send_thread_message(client, event, response, settings.dry_run)
            return Propagation.STOP

    if thread_has_context or is_mention:
        try:
            indicator_id = await _send_thread_message(
                client,
                event,
                get_msg("analyzing_followup"),
                settings.dry_run,
            )
        except Exception as exc:
            logger.warning("Could not send Time follow-up indicator: %s", exc)
            indicator_id = None
        if not cleaned_text:
            if indicator_id:
                try:
                    await _delete_post(client, indicator_id)
                except Exception as exc:
                    logger.warning("Could not delete Time follow-up indicator: %s", exc)
            return Propagation.STOP

        async def report_tool_failure(_failed_tools: list[str]) -> None:
            if indicator_id and not settings.dry_run:
                await _edit_post(client, indicator_id, get_msg("mcp_failure_progress"))

        answer, new_incident_id, fp_used, rejection = await handle_followup_question(
            reply_to_id=root_id if thread_has_context else None,
            question=cleaned_text,
            user_id=post.user_id,
            chat_id=f"time:{post.channel_id}",
            user_display_name=await _get_user_display_name(client, post.user_id),
            on_tool_failure=report_tool_failure,
        )

        if rejection == RejectionReason.NO_CONTEXT:
            if indicator_id:
                try:
                    await _delete_post(client, indicator_id)
                except Exception as exc:
                    logger.warning("Could not delete Time follow-up indicator: %s", exc)
            return Propagation.STOP

        if rejection == RejectionReason.COOLDOWN:
            answer = get_msg("cooldown")
        elif rejection == RejectionReason.LIMIT_REACHED:
            max_turns = settings.followup_max_turns
            answer = get_msg("limit_reached").format(current=max_turns, max=max_turns)

        final_post_id = await _finish_followup(
            client,
            event,
            indicator_id,
            answer,
            settings.dry_run,
        )
        if final_post_id and new_incident_id and fp_used:
            await store.register_bot_message(final_post_id, fp_used, incident_id=new_incident_id)
            thread_root_id = _thread_root(event)
            if thread_root_id != final_post_id:
                await store.register_bot_message(
                    thread_root_id,
                    fp_used,
                    incident_id=new_incident_id,
                )
            await store.set_last_active_incident(f"time:{post.channel_id}", fp_used)
        return Propagation.STOP

    if root_id:
        return Propagation.STOP

    await process_alert_text(text, _handle_alert_group, event, client)
    return Propagation.STOP


def register_handlers(
    router: Router,
    settings: Settings,
    client: TimeClient,
    identity: TimeBotIdentity,
) -> None:
    """Register the Time ``posted`` event handler on an aiotimebot router."""

    @router.on(EventType.POSTED)
    async def posted(event: PostedEvent, context: HandlerContext) -> Propagation:
        try:
            return await handle_posted_event(event, client, settings, identity)
        except Exception:
            logger.exception("Unhandled error while processing Time post %s", event.post.id)
            raise
