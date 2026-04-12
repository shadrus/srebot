"""Utility for sanitizing and repairing HTML for Telegram."""

import logging
import markdown
from lxml import html

logger = logging.getLogger(__name__)

# List of tags supported by Telegram's HTML parse mode
# See: https://core.telegram.org/bots/api#html-style
ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "span",
    "a",
    "code",
    "pre",
    "blockquote",
}

# Tags that MUST NOT contain any children tags per Telegram rules
RECURSIVE_FORBIDDEN = {"code", "pre"}


def markdown_to_telegram_html(md_text: str) -> str:
    """
    Convert standard Markdown to Telegram-compatible HTML.

    1. Convert MD to HTML using standard library.
    2. Clean and repair the resulting HTML using clean_telegram_html.
    """
    if not md_text:
        return ""

    # Convert markdown to HTML. Use 'extra' for tables/fenced code blocks.
    raw_html = markdown.markdown(md_text, extensions=["extra"])
    return clean_telegram_html(raw_html)


def clean_telegram_html(text: str) -> str:
    """
    Repair and sanitize HTML string to be compatible with Telegram's strict parser.
    """
    if not text:
        return ""

    try:
        # We use a fragment parser. Wrapping in a tag helps lxml handle it correctly.
        fragment = html.fragment_fromstring(text, create_parent="div")
    except Exception as exc:
        logger.warning("Failed to parse HTML fragment: %s. Returning escaped text.", exc)
        import html as std_html
        return std_html.escape(text)

    # 1. First Pass: Handle recursive forbidden tags (code, pre)
    for tag_name in RECURSIVE_FORBIDDEN:
        for node in fragment.xpath(f".//{tag_name}"):
            # Extract content including tags
            inner_content = (node.text or "") + "".join(
                html.tostring(child, encoding="unicode") for child in node
            )
            # Clear node
            for child in list(node):
                node.remove(child)
            # Set escaped content as text
            node.text = inner_content

    # 2. Second Pass: Remove unsupported tags but keep content (unwrap)
    for node in list(fragment.iter()):
        if node is fragment:
            continue

        should_unwrap = False
        if node.tag not in ALLOWED_TAGS:
            should_unwrap = True
        elif node.tag == "span" and node.get("class") != "tg-spoiler":
            should_unwrap = True

        if should_unwrap:
            parent = node.getparent()
            if parent is not None:
                index = parent.index(node)
                if node.text:
                    if index == 0:
                        parent.text = (parent.text or "") + node.text
                    else:
                        prev = parent[index - 1]
                        prev.tail = (prev.tail or "") + node.text
                for child in list(node):
                    parent.insert(index + 1, child)
                    index += 1
                if node.tail:
                    last_index = parent.index(node)
                    if last_index < len(parent) - 1:
                        next_node = parent[last_index + 1]
                        next_node.text = node.tail + (next_node.text or "")
                    else:
                        parent.tail = (parent.tail or "") + node.tail
                parent.remove(node)

    # Convert back to string
    result = html.tostring(fragment, encoding="unicode", method="html")

    # Strip the wrapping <div>
    if result.startswith("<div>"):
        result = result[5:]
    if result.endswith("</div>"):
        result = result[:-6]

    return result
