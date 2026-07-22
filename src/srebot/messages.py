"""Shared localized messages and platform-specific chat markup rendering."""

from typing import Literal

MessageFormat = Literal["markdown", "telegram", "slack", "discord", "time"]

_MESSAGES = {
    "Russian": {
        "analyzing_alerts": "🔍 [bold]Анализирую {count} алерт(ов)…[/bold]",
        "ttl_footer": (
            "\n\n[italic]💬 Задайте уточняющие вопросы ответом на это сообщение "
            "в течение {hours} ч.[/italic]"
        ),
        "analyzing_followup": "🔍 [italic]Анализирую...[/italic]",
        "mcp_failure_progress": (
            "⚠️ [italic]Часть источников данных недоступна. "
            "Продолжаю анализ по доступным данным...[/italic]"
        ),
        "tools_used": "[bold]🛠 Использованные инструменты:[/bold] {tools}",
        "mcp_failure_result": (
            "⚠️ [bold]Часть источников данных недоступна.[/bold] "
            "Не удалось выполнить: {tools}. Выводы анализа могут быть неполными."
        ),
        "cooldown": "⏳ [italic]Подождите немного перед следующим вопросом.[/italic]",
        "limit_reached": (
            "🔒 [italic]Лимит уточняющих вопросов по этому инциденту исчерпан "
            "({current}/{max}).[/italic]"
        ),
        "resolved_alert": (
            "✅ [bold]Решено:[/bold] [code]{alertname}[/code]\n"
            "[bold]Кластер:[/bold] {cluster} | [bold]Job:[/bold] {job}"
        ),
        "new_alert": (
            "🚨 [bold]Новый алерт:[/bold] [code]{alertname}[/code]\n"
            "[bold]Кластер:[/bold] {cluster} | [bold]Job:[/bold] {job}\n\n"
            "[italic]💬 Ответьте на это сообщение, чтобы запустить AI-анализ.[/italic]"
        ),
    },
    "English": {
        "analyzing_alerts": "🔍 [bold]Analyzing {count} alert(s)…[/bold]",
        "ttl_footer": (
            "\n\n[italic]💬 Ask follow-up questions by replying to this message "
            "within {hours} h.[/italic]"
        ),
        "analyzing_followup": "🔍 [italic]Analyzing...[/italic]",
        "mcp_failure_progress": (
            "⚠️ [italic]Some data sources are unavailable. "
            "Continuing with available data...[/italic]"
        ),
        "tools_used": "[bold]🛠 Tools used:[/bold] {tools}",
        "mcp_failure_result": (
            "⚠️ [bold]Some data sources were unavailable.[/bold] "
            "Failed tools: {tools}. The analysis may be incomplete."
        ),
        "cooldown": "⏳ [italic]Please wait a bit before the next question.[/italic]",
        "limit_reached": (
            "🔒 [italic]Limit of follow-up questions for this incident reached "
            "({current}/{max}).[/italic]"
        ),
        "resolved_alert": (
            "✅ [bold]Resolved:[/bold] [code]{alertname}[/code]\n"
            "[bold]Cluster:[/bold] {cluster} | [bold]Job:[/bold] {job}"
        ),
        "new_alert": (
            "🚨 [bold]New Alert:[/bold] [code]{alertname}[/code]\n"
            "[bold]Cluster:[/bold] {cluster} | [bold]Job:[/bold] {job}\n\n"
            "[italic]💬 Reply to this message to run AI analysis.[/italic]"
        ),
    },
}

_MARKUP = {
    "markdown": {"bold": ("**", "**"), "italic": ("*", "*"), "code": ("`", "`")},
    "telegram": {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "code": ("<code>", "</code>")},  # noqa: E501
    "slack": {"bold": ("*", "*"), "italic": ("_", "_"), "code": ("`", "`")},
    "discord": {"bold": ("**", "**"), "italic": ("*", "*"), "code": ("`", "`")},
    "time": {"bold": ("**", "**"), "italic": ("_", "_"), "code": ("`", "`")},
}


def get_chat_message(key: str, language: str, message_format: MessageFormat) -> str:
    """
    Return a localized message rendered for a chat platform.

    Args:
        key: Message identifier.
        language: Configured response language, with English as fallback.
        message_format: Target chat markup format.

    Returns:
        Localized and platform-formatted message, or an empty string for an unknown key.
    """
    template = _MESSAGES.get(language, _MESSAGES["English"]).get(key, "")
    for tag, (opening, closing) in _MARKUP[message_format].items():
        template = template.replace(f"[{tag}]", opening).replace(f"[/{tag}]", closing)
    return template
