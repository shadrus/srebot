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
                    f"🔍 *Analyzing {len(alerts)} alert(s)...*\n"
                    f"`{primary.alertname}` · {primary.cluster} · {primary.labels.get('job', '')}"
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
        analysis = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ *Analysis failed:* `{exc}`\nPlease investigate manually."

    # Convert Markdown to Slack mrkdwn
    analysis = _markdown_to_slack(analysis)

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
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


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
        await _process_incoming_text(event.get("text", ""), event.get("channel", ""), client)

    @app.message()
    async def handle_message_events(event: dict, message: dict, client: AsyncWebClient) -> None:
        """
        Handler for all channel messages.

        Filters by configured channel, parses alerts, groups them
        and triggers analysis.
        """
        await _process_incoming_text(message.get("text", ""), event.get("channel", ""), client)
