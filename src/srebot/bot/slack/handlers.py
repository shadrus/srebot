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

# HTML tags produced by the SaaS LLM agent → Slack mrkdwn equivalents.
# Anchor tags are handled separately because the resulting <url|text> format
# would be incorrectly stripped by the generic tag-removal pattern.
_ANCHOR_RE = re.compile(r'<a href="(.*?)".*?>(.*?)</a>', re.DOTALL)
_ANCHOR_PLACEHOLDER = "\x00SLACK_LINK({url}|{text})\x00"
_ANCHOR_PLACEHOLDER_RE = re.compile(r"\x00SLACK_LINK\((.*?)\|(.*?)\)\x00", re.DOTALL)

_HTML_TO_SLACK = [
    (re.compile(r"<b>(.*?)</b>", re.DOTALL), r"*\1*"),
    (re.compile(r"<strong>(.*?)</strong>", re.DOTALL), r"*\1*"),
    (re.compile(r"<i>(.*?)</i>", re.DOTALL), r"_\1_"),
    (re.compile(r"<em>(.*?)</em>", re.DOTALL), r"_\1_"),
    (re.compile(r"<code>(.*?)</code>", re.DOTALL), r"`\1`"),
    (re.compile(r"<pre>(.*?)</pre>", re.DOTALL), r"```\1```"),
    (re.compile(r"<br\s*/?>", re.IGNORECASE), "\n"),
    # Strip any remaining unknown tags
    (re.compile(r"<[^>]+>"), ""),
]


def _html_to_slack(text: str) -> str:
    """
    Convert HTML produced by the SaaS agent to Slack mrkdwn format.

    Args:
        text: HTML-formatted string from the LLM response.

    Returns:
        Slack mrkdwn-formatted string.
    """

    # Step 1: protect anchor tags with a placeholder before generic strip
    def _anchor_to_placeholder(m: re.Match) -> str:
        return _ANCHOR_PLACEHOLDER.format(url=m.group(1), text=m.group(2))

    text = _ANCHOR_RE.sub(_anchor_to_placeholder, text)

    # Step 2: apply all other transformations (incl. generic tag strip)
    for pattern, replacement in _HTML_TO_SLACK:
        text = pattern.sub(replacement, text)

    # Step 3: restore anchors as Slack mrkdwn links
    text = _ANCHOR_PLACEHOLDER_RE.sub(lambda m: f"<{m.group(1)}|{m.group(2)}>", text)

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
        already_tracked = await store.get_reply_message_id(group_fp)
        await store.mark_resolved(group_fp)
        logger.info("Resolved group [%s]: %s", group_fp, label)

        if already_tracked:
            text = (
                f"✅ *Resolved:* `{primary.alertname}`\n"
                f"*Cluster:* {primary.cluster} | "
                f"*Job:* {primary.labels.get('job', '—')}"
            )
            if dry_run:
                logger.info("[DRY-RUN] Would send Slack message:\n%s", text)
            else:
                try:
                    await client.chat_postMessage(channel=channel_id, text=text)
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
        except Exception as exc:
            logger.error("Failed to send placeholder reply: %s", exc)
            return
    else:
        logger.info("[DRY-RUN] Analyzing group %s: %d alert(s)", group_fp, len(alerts))

    # Run LLM analysis
    try:
        analysis = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ *Analysis failed:* `{exc}`\nPlease investigate manually."

    # Convert HTML from agent to Slack mrkdwn
    analysis = _html_to_slack(analysis)

    if dry_run:
        logger.info("[DRY-RUN] Analysis result for group %s:\n%s", group_fp, analysis)
        await store.mark_firing(group_fp, reply_message_id=0)
        return

    # Edit placeholder with full analysis
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

    if placeholder_ts:
        await store.mark_firing(group_fp, placeholder_ts)


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
