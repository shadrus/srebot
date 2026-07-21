"""Universal command processing and muting support for SREBot."""

import logging
import re

from srebot.config import get_settings
from srebot.state.store import get_store

logger = logging.getLogger(__name__)

MUTE_RE = re.compile(
    r"^(?:/)?mute(?:@(\w+))?(?:\s+((?:\d+\s*(?:s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?)\s*)+))?$",
    re.IGNORECASE,
)
UNMUTE_RE = re.compile(r"^(?:/)?unmute(?:@(\w+))?$", re.IGNORECASE)
STATUS_RE = re.compile(r"^(?:/)?status(?:@(\w+))?$", re.IGNORECASE)

COMMAND_MESSAGES = {
    "Russian": {
        "mute_global_success": "🔕 **Режим тишины активирован на {duration_desc}** для всех тревог.",  # noqa: E501
        "mute_alert_success": "🔕 **Режим тишины активирован на {duration_desc}** для типа тревог `{alertname}`.",  # noqa: E501
        "unmute_global_success": "🔔 **Режим тишины отключен.** Все тревоги снова анализируются.",
        "unmute_alert_success": "🔔 **Режим тишины отключен** для типа тревог `{alertname}`.",
        "invalid_duration": "⚠️ Неверный формат длительности: '{duration}'. Используйте, например: `1h`, `30m`, `1d`.",  # noqa: E501
        "no_alert_context": "⚠️ Не удалось определить тип тревоги из контекста ответа. Команда должна быть отправлена в ответ на сообщение бота с анализом тревоги (RCA), чтобы применить её только к этому типу.",  # noqa: E501
        "status_no_mutes": "🔊 **Активных режимов тишины в этом чате нет.** Бот анализирует все поступающие тревоги.",  # noqa: E501
        "status_mutes_header": "🚫 **Активные режимы тишины в этом чате**:\n",
        "status_global_item": "- **Глобальный режим тишины**: активен (осталось {ttl_desc})\n",
        "status_alert_item": "- **Тревога `{alertname}`**: заглушена (осталось {ttl_desc})\n",
    },
    "English": {
        "mute_global_success": "🔕 **Silence mode activated for {duration_desc}** for all alerts.",
        "mute_alert_success": "🔕 **Silence mode activated for {duration_desc}** for alert type `{alertname}`.",  # noqa: E501
        "unmute_global_success": "🔔 **Silence mode deactivated.** All alerts will be analyzed.",
        "unmute_alert_success": "🔔 **Silence mode deactivated** for alert type `{alertname}`.",
        "invalid_duration": "⚠️ Invalid duration format: '{duration}'. Use e.g. `1h`, `30m`, `1d`.",
        "no_alert_context": "⚠️ Could not determine alert type from reply context. The command must be sent in reply to the bot's analysis (RCA) message to apply it to this type only.",  # noqa: E501
        "status_no_mutes": "🔊 **No active silence modes in this chat.** The bot is analyzing all incoming alerts.",  # noqa: E501
        "status_mutes_header": "🚫 **Active silence modes in this chat**:\n",
        "status_global_item": "- **Global silence**: active ({ttl_desc} remaining)\n",
        "status_alert_item": "- **Alert `{alertname}`**: silenced ({ttl_desc} remaining)\n",
    },
}


def get_msg(key: str) -> str:
    lang = get_settings().llm_response_language
    return COMMAND_MESSAGES.get(lang, COMMAND_MESSAGES["English"]).get(key, "")


def parse_duration(duration_str: str) -> int:
    """Parse a duration string (e.g. '1h', '30m', '1d') into seconds.

    Supports s, m, h, d. Default to 3600 (1 hour) if empty.
    Raises ValueError if format is invalid.
    """
    duration_str = duration_str.strip().lower()
    if not duration_str:
        return 3600

    pattern = re.compile(r"(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?)")
    matches = pattern.findall(duration_str)
    if not matches:
        raise ValueError(duration_str)

    total_seconds = 0
    for value_str, unit in matches:
        value = int(value_str)
        if unit.startswith("s"):
            total_seconds += value
        elif unit.startswith("m"):
            total_seconds += value * 60
        elif unit.startswith("h"):
            total_seconds += value * 3600
        elif unit.startswith("d"):
            total_seconds += value * 86400

    return total_seconds


def extract_chat_id(*handler_args) -> str | None:
    """Extract a universal platform-specific chat/channel ID string from handler arguments."""
    if not handler_args:
        return None
    arg = handler_args[0]

    # Telegram Message
    if hasattr(arg, "chat_id"):
        return f"telegram:{arg.chat_id}"

    # Discord Message
    if hasattr(arg, "channel") and hasattr(arg.channel, "id"):
        return f"discord:{arg.channel.id}"

    # Time Messenger PostedEvent
    if hasattr(arg, "post") and hasattr(arg.post, "channel_id"):
        return f"time:{arg.post.channel_id}"

    # Slack channel ID is passed directly as a string in handler_args
    if isinstance(arg, str):
        return f"slack:{arg}"

    return None


def is_command_message(text: str) -> bool:
    """Check if the text is a command (mute/unmute/status, with or without slash)."""
    text_stripped = text.strip()
    return bool(
        MUTE_RE.match(text_stripped)
        or UNMUTE_RE.match(text_stripped)
        or STATUS_RE.match(text_stripped)
    )


async def get_alertname_from_reply(reply_to_id: str | None) -> str | None:
    """Retrieve the alertname associated with a bot reply message ID."""
    if not reply_to_id:
        return None
    store = await get_store()
    fp = await store.get_fp_by_message_id(reply_to_id)
    if not fp:
        return None
    ctx = await store.get_followup_context(fp)
    if not ctx or not ctx.get("alert_data"):
        return None
    return ctx["alert_data"][0].get("alertname")


async def handle_command(
    text: str,
    reply_to_id: str | None,
    chat_id: str,
    bot_username: str | None = None,
) -> str | None:
    """Execute a command if the text matches command patterns.

    Args:
        text: Raw text of the incoming message.
        reply_to_id: ID of the message being replied to, if any.
        chat_id: Unified identifier of the chat/channel.
        bot_username: The username of the current bot instance (optional).

    Returns:
        The markdown response message if a command was executed, or None if the text
        is not a recognized command or targets a different bot.
    """
    text_stripped = text.strip()

    # 1. Match MUTE
    mute_match = MUTE_RE.match(text_stripped)
    if mute_match:
        target_bot = mute_match.group(1)
        if target_bot and bot_username and target_bot.lower() != bot_username.lower():
            return None

        duration_raw = mute_match.group(2) or ""
        try:
            duration_seconds = parse_duration(duration_raw)
        except ValueError:
            return get_msg("invalid_duration").format(duration=duration_raw)

        duration_desc = duration_raw.strip() if duration_raw.strip() else "1h"
        store = await get_store()

        # Check if reply
        if reply_to_id:
            alertname = await get_alertname_from_reply(reply_to_id)
            if not alertname:
                return get_msg("no_alert_context")

            await store.set_alert_mute(chat_id, alertname, duration_seconds)
            return get_msg("mute_alert_success").format(
                duration_desc=duration_desc, alertname=alertname
            )
        else:
            await store.set_global_mute(chat_id, duration_seconds)
            return get_msg("mute_global_success").format(duration_desc=duration_desc)

    # 2. Match UNMUTE
    unmute_match = UNMUTE_RE.match(text_stripped)
    if unmute_match:
        target_bot = unmute_match.group(1)
        if target_bot and bot_username and target_bot.lower() != bot_username.lower():
            return None

        store = await get_store()

        # Check if reply
        if reply_to_id:
            alertname = await get_alertname_from_reply(reply_to_id)
            if not alertname:
                return get_msg("no_alert_context")

            await store.clear_alert_mute(chat_id, alertname)
            return get_msg("unmute_alert_success").format(alertname=alertname)
        else:
            await store.clear_all_mutes(chat_id)
            return get_msg("unmute_global_success")

    # 3. Match STATUS
    status_match = STATUS_RE.match(text_stripped)
    if status_match:
        target_bot = status_match.group(1)
        if target_bot and bot_username and target_bot.lower() != bot_username.lower():
            return None

        store = await get_store()
        mutes = await store.get_active_mutes(chat_id)

        global_ttl = mutes.get("global_ttl")
        alerts = mutes.get("alerts") or {}

        if global_ttl is None and not alerts:
            return get_msg("status_no_mutes")

        lines = [get_msg("status_mutes_header")]
        if global_ttl is not None:
            # global_ttl is int
            lines.append(get_msg("status_global_item").format(ttl_desc=format_ttl(int(global_ttl))))

        for alertname, ttl in sorted(alerts.items()):
            lines.append(
                get_msg("status_alert_item").format(
                    alertname=alertname, ttl_desc=format_ttl(int(ttl))
                )
            )

        return "".join(lines).strip()

    return None


def format_ttl(seconds: int) -> str:
    """Format seconds into a human-readable duration (e.g. 1h 30m)."""
    if seconds <= 0:
        return "0s"
    parts = []
    d, r = divmod(seconds, 86400)
    if d > 0:
        parts.append(f"{d}d")
    h, r = divmod(r, 3600)
    if h > 0 or d > 0:
        parts.append(f"{h}h")
    m, s = divmod(r, 60)
    if m > 0 or h > 0 or d > 0:
        parts.append(f"{m}m")
    if s > 0 or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)
