import pytest

from srebot.bot.messages import get_chat_message


@pytest.mark.parametrize(
    ("message_format", "bold", "italic", "code"),
    [
        ("telegram", ("<b>", "</b>"), ("<i>", "</i>"), ("<code>", "</code>")),
        ("slack", ("*", "*"), ("_", "_"), ("`", "`")),
        ("discord", ("**", "**"), ("*", "*"), ("`", "`")),
        ("time", ("**", "**"), ("_", "_"), ("`", "`")),
    ],
)
def test_renders_shared_message_with_platform_markup(message_format, bold, italic, code):
    message = get_chat_message("new_alert", "English", message_format)

    assert f"{bold[0]}New Alert:{bold[1]}" in message
    assert f"{code[0]}{{alertname}}{code[1]}" in message
    assert f"{italic[0]}💬 Reply to this message" in message
    assert message.endswith(italic[1])
    assert "[bold]" not in message


def test_uses_english_fallback_and_preserves_format_placeholders():
    message = get_chat_message("limit_reached", "Unsupported", "telegram")

    assert "Limit of follow-up questions" in message
    assert "{current}/{max}" in message


def test_returns_empty_string_for_unknown_message():
    assert get_chat_message("unknown", "Russian", "discord") == ""
