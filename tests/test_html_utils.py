from srebot.bot.telegram.html_utils import markdown_to_telegram_html

def test_markdown_to_telegram_bold():
    text = "This is **bold** and *italic*"
    result = markdown_to_telegram_html(text)
    # markdown library converts ** to <strong> or <b> depending on config
    # our clean_telegram_html handles both.
    assert "<strong>bold</strong>" in result or "<b>bold</b>" in result
    assert "<em>italic</em>" in result or "<i>italic</i>" in result

def test_markdown_to_telegram_headers():
    text = "# Header 1\n## Header 2"
    result = markdown_to_telegram_html(text)
    # Telegram doesn't support <h1>, so they should be unwrapped or removed
    # according to our cleaning logic (keeping text).
    assert "Header 1" in result
    assert "<h1>" not in result
    assert "<h2>" not in result

def test_markdown_to_telegram_code():
    text = "Check `api-server`"
    result = markdown_to_telegram_html(text)
    assert "<code>api-server</code>" in result

def test_markdown_to_telegram_fenced_code():
    text = "```\nlogs\n```"
    result = markdown_to_telegram_html(text)
    # markdown-extra converts fenced to <pre><code>
    # our logic: <code> inside <pre> is allowed or handled?
    # Telegram: <pre> is fine, <code> is fine.
    assert "<pre>" in result

def test_markdown_nested_in_code_fix():
    # LLM outputting tags inside code block (very common mistake)
    text = "Result: `<b>Forbidden</b>`"
    result = markdown_to_telegram_html(text)
    # html_utils.py should ensure the <b> is escaped inside <code>
    assert "<code>&lt;b&gt;Forbidden&lt;/b&gt;</code>" in result

def test_stray_angle_brackets_markdown():
    text = "If x < 5 or y > 10"
    result = markdown_to_telegram_html(text)
    assert "x &lt; 5" in result
    assert "y &gt; 10" in result
