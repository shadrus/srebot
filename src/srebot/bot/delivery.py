"""Platform-neutral pagination and ordered delivery for chat responses."""

import asyncio
import enum
import html
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

import regex

logger = logging.getLogger(__name__)

RenderMessage = Callable[[str], str]
MeasureMessage = Callable[[str], int]
SendChunk = Callable[[str, str | None], Awaitable[str | int]]
EditFirstChunk = Callable[[str], Awaitable[str | int]]

_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<info>[^\n]*)\n?",
    re.MULTILINE,
)
_LINK_RE = re.compile(r"\[([^\]]+)]\(([^)]+)\)")


def _delivery_failure_reason(exc: Exception) -> str:
    """Classify terminal delivery failures for structured observability."""
    response = getattr(exc, "response", None)
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(response, "status_code", None)
    if status_code is None and isinstance(response, dict):
        status_code = response.get("status_code")
    exception_name = exc.__class__.__name__
    message = "" if exception_name == "RetryAfter" else str(exc).lower()
    if (
        status_code == 429
        or exception_name == "RetryAfter"
        or hasattr(exc, "retry_after")
        or "flood control exceeded" in message
        or ("exhaust" in message and ("retry" in message or "rate limit" in message))
    ):
        return "retry_exhausted"
    return "operation_failed"


class DeliveryStatus(enum.StrEnum):
    """Terminal state of a message delivery batch."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MessageConstraints:
    """Size and measurement rules for one chat platform."""

    max_chars: int
    format_name: str
    measure: MeasureMessage = len

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if not self.format_name:
            raise ValueError("format_name must not be empty")


@dataclass(frozen=True, slots=True)
class MessageChunk:
    """One independently rendered page of a logical response."""

    markdown: str
    rendered: str
    index: int
    total: int

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("index must be at least 1")
        if self.total < self.index:
            raise ValueError("total must be greater than or equal to index")


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Result of delivering an ordered batch of chunks."""

    status: DeliveryStatus
    message_ids: tuple[str, ...]
    expected_chunks: int

    def __post_init__(self) -> None:
        if self.expected_chunks < 0:
            raise ValueError("expected_chunks cannot be negative")
        if len(self.message_ids) > self.expected_chunks:
            raise ValueError("message_ids cannot exceed expected_chunks")
        expected_status = (
            DeliveryStatus.COMPLETE
            if self.expected_chunks > 0 and len(self.message_ids) == self.expected_chunks
            else DeliveryStatus.PARTIAL
            if self.message_ids
            else DeliveryStatus.FAILED
        )
        if self.status != expected_status:
            raise ValueError("status does not match delivered and expected chunk counts")

    @classmethod
    def from_ids(cls, message_ids: Sequence[str | int], expected_chunks: int) -> DeliveryReceipt:
        """Build a receipt and derive its terminal status."""
        normalized = tuple(str(message_id) for message_id in message_ids)
        status = (
            DeliveryStatus.COMPLETE
            if expected_chunks > 0 and len(normalized) == expected_chunks
            else DeliveryStatus.PARTIAL
            if normalized
            else DeliveryStatus.FAILED
        )
        return cls(status=status, message_ids=normalized, expected_chunks=expected_chunks)

    @property
    def primary_message_id(self) -> str | None:
        """Return the canonical first delivered message ID."""
        return self.message_ids[0] if self.message_ids else None

    @property
    def delivered_chunks(self) -> int:
        """Return the number of successfully delivered chunks."""
        return len(self.message_ids)


class _VisibleHTMLParser(HTMLParser):
    """Count text after HTML entity and tag parsing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_visible_length(value: str) -> int:
    """Return the visible character count of rendered HTML."""
    parser = _VisibleHTMLParser()
    parser.feed(value)
    parser.close()
    return len("".join(parser.parts))


def _graphemes(text: str) -> list[str]:
    """Split text into Unicode extended grapheme clusters per UAX #29."""
    return regex.findall(r"\X", text)


def _is_closing_fence(line: str, opening_marker: str) -> bool:
    """Return whether a line is a valid close for the opening fence marker."""
    leading_spaces = len(line) - len(line.lstrip(" "))
    if leading_spaces > 3:
        return False
    stripped = line[leading_spaces:]
    marker = re.escape(opening_marker[0])
    return re.fullmatch(rf"{marker}{{{len(opening_marker)},}}[ \t]*", stripped) is not None


def _markdown_blocks(text: str) -> list[str]:
    """Tokenize Markdown into blank-line-delimited blocks while keeping fences intact."""
    if not text:
        return []
    lines = text.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    fence_marker: str | None = None

    def flush() -> None:
        if current:
            blocks.append("\n".join(current))
            current.clear()

    for line in lines:
        stripped = line.lstrip()
        fence_match = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence_marker:
            current.append(line)
            if _is_closing_fence(line, fence_marker):
                flush()
                fence_marker = None
            continue
        if fence_match:
            flush()
            fence_marker = fence_match.group(1)
            current.append(line)
            continue
        if not stripped:
            flush()
            continue
        current.append(line)
    flush()
    return blocks


def canonicalize_fence_indentation(text: str) -> str:
    """Remove optional CommonMark fence indentation without changing code lines."""
    lines: list[str] = []
    fence_marker: str | None = None
    for line_with_ending in text.splitlines(keepends=True):
        line = line_with_ending.rstrip("\r\n")
        ending = line_with_ending[len(line) :]
        if fence_marker is not None:
            if _is_closing_fence(line, fence_marker):
                line = line.lstrip(" ")
                fence_marker = None
        else:
            match = re.match(r" {1,3}(`{3,}|~{3,})", line)
            if match:
                fence_marker = match.group(1)
                line = line.lstrip(" ")
        lines.append(f"{line}{ending}")
    return "".join(lines)


def _balanced_inline_prefix(text: str) -> bool:
    """Return whether a prefix ends outside common inline Markdown constructs."""
    escaped = False
    backticks = 0
    square_depth = 0
    paren_depth = 0
    bold = 0
    emphasis = 0
    strikethrough = 0
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
        elif char == "`":
            backticks ^= 1
        elif not backticks:
            if text.startswith("**", index) or text.startswith("__", index):
                bold ^= 1
                index += 1
            elif text.startswith("~~", index):
                strikethrough ^= 1
                index += 1
            elif char in "*_":
                emphasis ^= 1
            elif char == "[":
                square_depth += 1
            elif char == "]" and square_depth:
                square_depth -= 1
            elif char == "(":
                paren_depth += 1
            elif char == ")" and paren_depth:
                paren_depth -= 1
        index += 1
    return not any((backticks, square_depth, paren_depth, bold, emphasis, strikethrough))


def _plain_inline(text: str) -> str:
    """Convert indivisible inline Markdown to safe, readable plain text."""
    text = _LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", text)
    text = re.sub(r"(`+)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*\*|__|~~)(.*?)\1", r"\2", text)
    text = re.sub(r"(?<!\\)([*_])([^*_]+)\1", r"\2", text)
    return html.unescape(text)


def _largest_fitting_prefix(
    text: str,
    constraints: MessageConstraints,
    render: RenderMessage,
) -> int:
    """Find the largest source prefix whose rendered form fits."""
    low = 1
    high = len(text)
    best = 0
    while low <= high:
        middle = (low + high) // 2
        if constraints.measure(render(text[:middle])) <= constraints.max_chars:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _safe_boundary(text: str, maximum: int, *, preserve_whitespace: bool = False) -> int:
    """Choose the best balanced line or whitespace boundary at or before maximum."""
    for matcher in (r"\n", r"\s"):
        positions = [
            match.end() if preserve_whitespace else match.start()
            for match in re.finditer(matcher, text[: maximum + 1])
            if (match.end() if preserve_whitespace else match.start()) <= maximum
        ]
        for position in reversed(positions):
            if position > 0 and (preserve_whitespace or _balanced_inline_prefix(text[:position])):
                return position
    return 0


def _split_plain(
    text: str,
    constraints: MessageConstraints,
    render: RenderMessage,
    *,
    preserve_whitespace: bool = False,
) -> list[str]:
    """Split plain text using safe boundaries and a grapheme fallback."""
    pieces: list[str] = []
    remaining = text
    while remaining:
        if constraints.measure(render(remaining)) <= constraints.max_chars:
            pieces.append(remaining)
            break
        maximum = _largest_fitting_prefix(remaining, constraints, render)
        if maximum <= 0:
            raise ValueError("renderer cannot fit even one source character")
        boundary = _safe_boundary(
            remaining,
            maximum,
            preserve_whitespace=preserve_whitespace,
        )
        if boundary <= 0:
            clusters = _graphemes(remaining)
            candidate = ""
            consumed = 0
            for cluster in clusters:
                if constraints.measure(render(candidate + cluster)) > constraints.max_chars:
                    break
                candidate += cluster
                consumed += len(cluster)
            if not candidate:
                raise ValueError("renderer cannot fit one grapheme")
            boundary = consumed
        pieces.append(remaining[:boundary])
        remaining = remaining[boundary:]
    return pieces


def _split_fence(
    block: str,
    constraints: MessageConstraints,
    render: RenderMessage,
) -> list[str] | None:
    """Split a fenced code block while closing and reopening each page."""
    match = _FENCE_RE.match(block)
    if not match:
        return None
    marker = match.group("marker")
    indent = match.group("indent")
    info = match.group("info").strip()
    opening = f"{indent}{marker}{info}\n"
    closing = f"\n{indent}{marker}"
    body = block[match.end() :]
    closing_line_start = body.rfind("\n")
    if closing_line_start >= 0 and _is_closing_fence(body[closing_line_start + 1 :], marker):
        body = body[:closing_line_start]
    elif _is_closing_fence(body, marker):
        body = ""

    def render_wrapped(value: str) -> str:
        return render(f"{opening}{value}{closing}")

    body_constraints = MessageConstraints(
        max_chars=constraints.max_chars,
        format_name=constraints.format_name,
        measure=constraints.measure,
    )
    try:
        parts = _split_plain(
            body,
            body_constraints,
            render_wrapped,
            preserve_whitespace=True,
        )
    except ValueError:
        return None
    return [f"{opening}{part}{closing}" for part in parts]


def _split_oversized_block(
    block: str,
    constraints: MessageConstraints,
    render: RenderMessage,
) -> list[str]:
    fenced = _split_fence(block, constraints, render)
    if fenced:
        return fenced
    lines = block.splitlines()
    if len(lines) >= 2 and re.fullmatch(
        r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*",
        lines[1],
    ):
        safe_table = _split_fence(f"```text\n{block}\n```", constraints, render)
        if safe_table:
            return safe_table
    pieces = _split_plain(block, constraints, render)
    if len(pieces) > 1 and any(not _balanced_inline_prefix(piece) for piece in pieces):
        plain = _plain_inline(block)
        fenced_plain = _split_fence(f"```text\n{plain}\n```", constraints, render)
        if fenced_plain:
            return fenced_plain
        pieces = _split_plain(plain, constraints, render)
    return pieces


def paginate_markdown(
    text: str,
    constraints: MessageConstraints,
    render: RenderMessage = lambda value: value,
) -> list[MessageChunk]:
    """Paginate Markdown and render every page independently."""
    if not text:
        return []
    if constraints.measure(render(text)) <= constraints.max_chars:
        return [MessageChunk(markdown=text, rendered=render(text), index=1, total=1)]

    pages: list[str] = []
    current = ""
    for block in _markdown_blocks(text):
        candidate = f"{current}\n\n{block}" if current else block
        if constraints.measure(render(candidate)) <= constraints.max_chars:
            current = candidate
            continue
        if current:
            pages.append(current)
            current = ""
        if constraints.measure(render(block)) <= constraints.max_chars:
            current = block
        else:
            pages.extend(_split_oversized_block(block, constraints, render))
    if current:
        pages.append(current)

    total = len(pages)
    chunks = [
        MessageChunk(markdown=page, rendered=render(page), index=index, total=total)
        for index, page in enumerate(pages, start=1)
    ]
    if any(constraints.measure(chunk.rendered) > constraints.max_chars for chunk in chunks):
        raise ValueError("pagination produced an oversized rendered chunk")
    return chunks


class DeliveryCoordinator:
    """Serialize delivery batches per platform channel."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, destination: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(destination)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[destination] = lock
            self._lock_users[destination] = self._lock_users.get(destination, 0) + 1
            return lock

    async def _release_lock(self, destination: str, lock: asyncio.Lock) -> None:
        """Release one destination-lock lease and discard an unused lock."""
        async with self._locks_guard:
            if self._locks.get(destination) is not lock:
                raise RuntimeError(f"delivery lock changed unexpectedly for {destination}")
            users = self._lock_users[destination] - 1
            if users == 0:
                del self._lock_users[destination]
                del self._locks[destination]
            else:
                self._lock_users[destination] = users

    async def deliver(
        self,
        destination: str,
        chunks: Sequence[MessageChunk],
        send_chunk: SendChunk,
        edit_first: EditFirstChunk | None = None,
    ) -> DeliveryReceipt:
        """Deliver chunks as one ordered batch and return all successful IDs."""
        if not destination:
            raise ValueError("destination must not be empty")
        if not chunks:
            return DeliveryReceipt.from_ids((), 0)

        lock = await self._lock_for(destination)
        try:
            async with lock:
                message_ids: list[str | int] = []
                primary_id: str | None = None
                for index, chunk in enumerate(chunks):
                    try:
                        if index == 0 and edit_first:
                            try:
                                message_id = await edit_first(chunk.rendered)
                            except Exception as exc:
                                if _delivery_failure_reason(exc) == "retry_exhausted":
                                    raise
                                logger.warning(
                                    "Could not edit first chunk for %s; sending a new message: %s",
                                    destination,
                                    exc,
                                )
                                message_id = await send_chunk(chunk.rendered, None)
                        else:
                            message_id = await send_chunk(chunk.rendered, primary_id)
                    except Exception as exc:
                        reason = _delivery_failure_reason(exc)
                        logger.error(
                            "Delivery stopped destination=%s reason=%s chunks=%d/%d error=%s",
                            destination,
                            reason,
                            len(message_ids),
                            len(chunks),
                            exc,
                        )
                        break
                    normalized_id = str(message_id)
                    message_ids.append(normalized_id)
                    primary_id = primary_id or normalized_id

                receipt = DeliveryReceipt.from_ids(message_ids, len(chunks))
                logger.info(
                    "Message delivery destination=%s status=%s chunks=%d/%d",
                    destination,
                    receipt.status,
                    receipt.delivered_chunks,
                    receipt.expected_chunks,
                )
                return receipt
        finally:
            await self._release_lock(destination, lock)


delivery_coordinator = DeliveryCoordinator()
