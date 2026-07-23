"""Time Messenger handlers backed by the shared alert and follow-up workflows."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from aiotimebot import EventType, HandlerContext, PostedEvent, Propagation, Router, TimeClient
from aiotimebot.api.api.posts import delete_post
from aiotimebot.api.models.app_error import AppError
from aiotimebot.errors import APIError

import srebot.config as config
import srebot.state.store as state_store
from srebot.bot.delivery import (
    DeliveryReceipt,
    MessageConstraints,
    delivery_coordinator,
    paginate_markdown,
)
from srebot.bot.shared import (
    ChatAdapter,
    RejectionReason,
    execute_alert_group_workflow,
    handle_followup_question,
    process_alert_text,
    register_followup_receipt,
    rejection_turn_limit,
)
from srebot.config import Settings
from srebot.messages import get_chat_message
from srebot.parser.alert_parser import Alert

logger = logging.getLogger(__name__)

TIME_MESSAGE_FALLBACK = 15_500


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
    """Edit a Time post through the client's header-aware bounded retry transport."""
    http_client = client.api.get_async_httpx_client()
    response = await http_client.put(
        f"/api/v4/posts/{quote(post_id, safe='')}/patch",
        json={"message": text},
    )
    if response.status_code == 200:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = (
        str(payload.get("message") or response.reason_phrase)
        if isinstance(payload, Mapping)
        else response.reason_phrase
    )
    raise APIError(
        message,
        status_code=response.status_code,
        request_id=response.headers.get("X-Request-Id"),
    )


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


def _time_constraints(client: TimeClient) -> MessageConstraints:
    """Return runtime Time message constraints discovered during startup."""
    limit = getattr(client, "message_limit", TIME_MESSAGE_FALLBACK)
    if not isinstance(limit, int) or limit <= 0:
        limit = TIME_MESSAGE_FALLBACK
    return MessageConstraints(limit, "time")


async def _deliver_text(
    client: TimeClient,
    event: PostedEvent,
    text: str,
    *,
    thread_root: str | None = None,
    placeholder_id: str | None = None,
) -> DeliveryReceipt:
    """Deliver one ordered Time response using runtime server constraints."""
    chunks = paginate_markdown(text, _time_constraints(client))

    async def send_chunk(rendered: str, primary_id: str | None) -> str:
        root_id = thread_root or primary_id
        kwargs = {"channel_id": event.post.channel_id, "text": rendered}
        if root_id is not None:
            kwargs["root_id"] = root_id
        post = await client.send_message(**kwargs)
        return str(post.id)

    async def edit_first(rendered: str) -> str:
        if placeholder_id is None:
            raise RuntimeError("no Time placeholder is available")
        await _edit_post(client, placeholder_id, rendered)
        return placeholder_id

    return await delivery_coordinator.deliver(
        f"time:{event.post.channel_id}",
        chunks,
        send_chunk,
        edit_first if placeholder_id is not None else None,
    )


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
                logger.info("[DRY-RUN] Would send Time resolved message:\n%s", text)
            else:
                await _deliver_text(
                    self.client,
                    self.event,
                    text,
                    thread_root=reply_to_id,
                )
        except Exception as exc:
            logger.warning("Could not send Time resolved reply: %s", exc)

    async def send_short_notification(
        self, group_fp: str, label: str, primary: Alert
    ) -> DeliveryReceipt:
        """Post the non-automatic-analysis notification."""
        text = get_chat_message(
            "new_alert",
            config.get_settings().llm_response_language,
            "markdown",
        ).format(
            alertname=primary.alertname,
            cluster=primary.cluster,
            job=primary.labels.get("job", "—"),
        )
        chunks = paginate_markdown(text, _time_constraints(self.client))
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Time short notification:\n%s", text)
            return DeliveryReceipt.from_ids((), len(chunks))
        try:
            return await _deliver_text(self.client, self.event, text)
        except Exception as exc:
            logger.error("Failed to send Time short notification: %s", exc)
            return DeliveryReceipt.from_ids((), len(chunks))

    async def send_analyzing_placeholder(
        self, group_fp: str, label: str, primary: Alert, alert_count: int
    ) -> DeliveryReceipt:
        """Post an analysis placeholder in the incoming alert thread."""
        text = (
            get_chat_message(
                "analyzing_alerts",
                config.get_settings().llm_response_language,
                "markdown",
            ).format(count=alert_count)
            + f"\n`{primary.alertname}` · {primary.cluster} · {primary.labels.get('job', '')}"
        )
        chunks = paginate_markdown(text, _time_constraints(self.client))
        if self.dry_run:
            logger.info("[DRY-RUN] Would send Time placeholder:\n%s", text)
            return DeliveryReceipt.from_ids((), len(chunks))
        try:
            return await _deliver_text(self.client, self.event, text)
        except Exception as exc:
            logger.error("Failed to send Time placeholder: %s", exc)
            return DeliveryReceipt.from_ids((), len(chunks))

    async def update_with_analysis(
        self,
        group_fp: str,
        placeholder_id: str | int | None,
        analysis: str,
        is_billing_error: bool,
    ) -> DeliveryReceipt:
        """Replace the placeholder with the analysis, falling back to a new reply."""
        if not is_billing_error:
            ttl_hours = config.get_settings().followup_ttl // 3600
            analysis += get_msg("ttl_footer").format(hours=ttl_hours)

        chunks = paginate_markdown(analysis, _time_constraints(self.client))
        if self.dry_run:
            return DeliveryReceipt.from_ids((), len(chunks))
        return await _deliver_text(
            self.client,
            self.event,
            analysis,
            placeholder_id=str(placeholder_id) if placeholder_id is not None else None,
        )


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
) -> DeliveryReceipt:
    """Edit the follow-up indicator or send a fresh thread reply."""
    if dry_run:
        logger.info("[DRY-RUN] Time follow-up response:\n%s", text)
        chunks = paginate_markdown(text, _time_constraints(client))
        return DeliveryReceipt.from_ids((), len(chunks))
    return await _deliver_text(
        client,
        event,
        text,
        thread_root=_thread_root(event),
        placeholder_id=indicator_id,
    )


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
            if settings.dry_run:
                logger.info("[DRY-RUN] Time command response:\n%s", response)
            else:
                await _deliver_text(
                    client,
                    event,
                    response,
                    thread_root=_thread_root(event),
                )
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
        elif rejection is not None:
            max_turns = rejection_turn_limit(settings, rejection)
            answer = get_msg("limit_reached").format(current=max_turns, max=max_turns)

        receipt = await _finish_followup(
            client,
            event,
            indicator_id,
            answer,
            settings.dry_run,
        )
        await register_followup_receipt(
            receipt,
            fp_used,
            new_incident_id,
            f"time:{post.channel_id}",
            additional_message_ids=(_thread_root(event),),
        )
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
