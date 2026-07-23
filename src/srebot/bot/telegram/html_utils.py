"""Utility for sanitizing and repairing HTML for Telegram."""

import html as std_html
import logging
import re

import markdown
from lxml import etree

from srebot.bot.delivery import canonicalize_fence_indentation

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
_STYLE_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "div",
    "footer",
    "header",
    "main",
    "nav",
    "p",
    "section",
}
_SAFE_LINK_RE = re.compile(r"^(?:https?://|tg://|mailto:|tel:)", re.IGNORECASE)
_LANGUAGE_CLASS_RE = re.compile(r"language-[A-Za-z0-9_.+-]+")


def markdown_to_telegram_html(md_text: str) -> str:
    """
    Convert standard Markdown to Telegram-compatible HTML.

    1. Convert MD to HTML using standard library.
    2. Clean and repair the resulting HTML using clean_telegram_html.
    """
    if not md_text:
        return ""

    # Convert markdown to HTML. Use 'extra' for tables/fenced code blocks.
    raw_html = markdown.markdown(canonicalize_fence_indentation(md_text), extensions=["extra"])
    return clean_telegram_html(raw_html)


def clean_telegram_html(text: str) -> str:
    """
    Repair and sanitize HTML string to be compatible with Telegram's strict parser.
    """
    if not text:
        return ""

    def escape_text(value: str | None) -> str:
        return std_html.escape(value or "", quote=False)

    def plain_text(node: etree._Element) -> str:
        return escape_text("".join(node.itertext()))

    def serialize_children(node: etree._Element, parent_tag: str | None = None) -> str:
        pieces = [escape_text(node.text)]
        for child in node:
            pieces.append(serialize_node(child, parent_tag))
            pieces.append(escape_text(child.tail))
        return "".join(pieces)

    def serialize_list(node: etree._Element, *, ordered: bool) -> str:
        try:
            number = int(node.get("start", "1"))
        except ValueError:
            number = 1
        lines: list[str] = []
        for child in node:
            if child.tag != "li":
                continue
            prefix = f"{number}. " if ordered else "- "
            content = serialize_children(child, "li").strip("\n")
            lines.append(f"{prefix}{content}\n")
            number += 1
        return "".join(lines)

    def serialize_table(node: etree._Element) -> str:
        lines: list[str] = []
        for row in node.iter("tr"):
            cells = [
                serialize_children(cell, "td").strip().replace("\n", " ")
                for cell in row
                if cell.tag in {"th", "td"}
            ]
            if cells:
                lines.append(f"| {' | '.join(cells)} |\n")
        return "".join(lines)

    def serialize_pre(node: etree._Element) -> str:
        children = list(node)
        if (
            len(children) == 1
            and children[0].tag == "code"
            and not (node.text or "").strip()
            and not (children[0].tail or "").strip()
        ):
            code = children[0]
            language_class = code.get("class", "")
            class_attr = (
                f' class="{language_class}"' if _LANGUAGE_CLASS_RE.fullmatch(language_class) else ""
            )
            return f"<pre><code{class_attr}>{plain_text(code)}</code></pre>"
        return f"<pre>{plain_text(node)}</pre>"

    def serialize_node(node: etree._Element, parent_tag: str | None = None) -> str:
        tag = node.tag if isinstance(node.tag, str) else ""
        if not tag:
            return ""
        if tag in _STYLE_TAGS:
            forbidden = {"a", "blockquote", "code", "pre"}
            if any(
                isinstance(descendant.tag, str) and descendant.tag in forbidden
                for descendant in node.iterdescendants()
            ):
                return serialize_children(node, parent_tag)
            return f"<{tag}>{serialize_children(node, tag)}</{tag}>"
        if tag == "span":
            content = serialize_children(node, "span")
            if node.get("class") == "tg-spoiler":
                return f'<span class="tg-spoiler">{content}</span>'
            return content
        if tag == "a":
            content = plain_text(node)
            href = node.get("href", "")
            if _SAFE_LINK_RE.match(href):
                return f'<a href="{std_html.escape(href, quote=True)}">{content}</a>'
            return content
        if tag == "code":
            return f"<code>{plain_text(node)}</code>"
        if tag == "pre":
            return serialize_pre(node)
        if tag == "blockquote":
            content = serialize_children(node, "blockquote")
            if parent_tag == "blockquote":
                return content
            expandable = " expandable" if "expandable" in node.attrib else ""
            return f"<blockquote{expandable}>{content}</blockquote>"
        if tag == "br":
            return "" if (node.tail or "").startswith(("\n", "\r")) else "\n"
        if tag == "ul":
            return serialize_list(node, ordered=False)
        if tag == "ol":
            return serialize_list(node, ordered=True)
        if tag == "li":
            return f"- {serialize_children(node, 'li').strip(chr(10))}\n"
        if tag == "table":
            return serialize_table(node)
        if tag == "hr":
            return "—\n"
        if tag in _BLOCK_TAGS or re.fullmatch(r"h[1-6]", tag):
            return f"{serialize_children(node, tag)}\n"
        if tag in {"thead", "tbody", "tfoot"}:
            return serialize_children(node, tag)
        if tag == "tr":
            return f"{serialize_children(node, tag)}\n"
        if tag in {"th", "td"}:
            return serialize_children(node, tag)
        return serialize_children(node, parent_tag)

    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
        fragment = etree.fromstring(f"<root>{text}</root>".encode(), parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        logger.warning("Failed to parse HTML fragment: %s. Returning escaped text.", exc)
        return std_html.escape(text)

    return serialize_children(fragment)
