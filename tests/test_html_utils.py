from xml.etree import ElementTree

import pytest
from lxml import html as lxml_html

from srebot.bot.telegram.html_utils import ALLOWED_TAGS, markdown_to_telegram_html


def _parse_strict_fragment(value: str) -> ElementTree.Element:
    """Parse without HTML error recovery so malformed output fails the test."""
    assert "</div>" not in value
    fragment = ElementTree.fromstring(f"<root>{value}</root>")
    for node in fragment.iter():
        if node is fragment:
            continue
        assert node.tag in ALLOWED_TAGS
        if node.tag == "a":
            assert set(node.attrib) <= {"href"}
        elif node.tag == "span":
            assert node.attrib == {"class": "tg-spoiler"}
        elif node.tag == "code" and node.get("class"):
            assert node.get("class", "").startswith("language-")
        elif node.tag == "blockquote":
            assert set(node.attrib) <= {"expandable"}
        else:
            assert not node.attrib
    return fragment


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
    text = "```python\nprint('<ok>')\n```"
    result = markdown_to_telegram_html(text)
    fragment = lxml_html.fromstring(f"<div>{result}</div>")

    assert fragment.xpath(".//pre/code[@class='language-python']")
    assert fragment.text_content() == "print('<ok>')\n"


@pytest.mark.parametrize("marker", ["```", "~~~"])
@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_markdown_to_telegram_canonicalizes_indented_fences(marker, indent):
    result = markdown_to_telegram_html(
        f"{indent}{marker}python\n**literal** [x](https://code.test)\n{indent}{marker}"
    )
    fragment = lxml_html.fromstring(f"<div>{result}</div>")

    assert fragment.xpath(".//pre/code[@class='language-python']")
    assert fragment.text_content() == "**literal** [x](https://code.test)\n"


@pytest.mark.parametrize(
    ("markdown_text", "visible_text"),
    [
        ("- a\n- b", "- a\n- b\n"),
        ("1. a\n2. b", "1. a\n2. b\n"),
        ("| A | B |\n|---|---|\n| x | y |", "| A | B |\n| x | y |\n"),
        ("a  \nb", "a\nb\n"),
    ],
)
def test_markdown_to_telegram_emits_strict_valid_html(markdown_text, visible_text):
    result = markdown_to_telegram_html(markdown_text)
    fragment = _parse_strict_fragment(result)

    assert "".join(fragment.itertext()) == visible_text


def test_markdown_to_telegram_flattens_entities_nested_in_pre():
    result = markdown_to_telegram_html("<pre><b>danger</b></pre>")

    fragment = _parse_strict_fragment(result)
    assert result == "<pre>danger</pre>"
    assert "".join(fragment.itertext()) == "danger"


def test_markdown_to_telegram_strips_attributes_and_invalid_link_nesting():
    result = markdown_to_telegram_html(
        '<b onclick="danger()">safe</b>\n\n[**linked**](https://example.test)'
    )

    fragment = _parse_strict_fragment(result)
    assert "onclick" not in result
    assert not fragment.findall(".//a/strong")


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
