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
                f"🔍 **Analyzing {len(alerts)} alert(s)**…\n"
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
        analysis = await agent.analyze(alerts)
    except Exception as exc:
        logger.exception("LLM analysis failed for group %s", group_fp)
        analysis = f"⚠️ **Analysis failed:** `{exc}`\nPlease investigate manually."

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
    else:
        logger.info(
            "Alert %s was %s during analysis. Not marking as firing.",
            group_fp,
            current_status,
        )


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

        logger.debug("Received message %d from channel_id=%d", message.id, message.channel.id)
        await process_alert_text(message.content, _handle_alert_group, message)
