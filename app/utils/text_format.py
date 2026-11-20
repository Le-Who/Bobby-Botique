import html
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

from telegram.constants import MessageLimit

MAX_MESSAGE_LENGTH = 4096
_MAX_FORMATTING_ENTITIES = MessageLimit.MESSAGE_ENTITIES

_TAG_NAMES = r"pre|code|b|i|a|u|s|em|strong|blockquote|ins|strike|del|span|tg-spoiler|tg-emoji"
_TAG_ATTRIBUTES = r"""(?:\s(?:"[^"]*"|'[^']*'|[^<>'"])*)?"""
_TAG_RE = re.compile(rf"<(/?)({_TAG_NAMES}){_TAG_ATTRIBUTES}>", re.IGNORECASE)
_EMPTY_TAG_RE = re.compile(rf"<({_TAG_NAMES}){_TAG_ATTRIBUTES}></\1>", re.IGNORECASE)
_ENTITY_PATTERN = r"&(?:[a-zA-Z][a-zA-Z0-9]{0,31}|#(?:[0-9]+|[xX][a-fA-F0-9]+));"
_HTML_TOKEN_RE = re.compile(rf"{_TAG_RE.pattern}|{_ENTITY_PATTERN}|[^<>&]+|[<>&]", re.IGNORECASE)

_CODE_BLOCK_RE = re.compile(r"(```(?:.|\n)*?```)")
_ESCAPE_CLEANUP_RE = re.compile(r"\\([.\-()!=[\]{}|#+])")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_HR_RE = re.compile(r"^[-*_]{3,}\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_UNDER_RE = re.compile(r"(?<!\w)__(.+?)__(?!\w)")
_PYTHON_DUNDER_NAMES = frozenset(dir(object)) | {
    "__all__",
    "__annotations__",
    "__builtins__",
    "__dict__",
    "__file__",
    "__loader__",
    "__module__",
    "__name__",
    "__package__",
    "__path__",
    "__spec__",
}
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_ITALIC_SINGLE_UNDER_RE = re.compile(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)")
_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_STRIP_HTML_RE = re.compile(r"<[^>]+>")


def _decode_entity(entity: str) -> str:
    if entity.startswith("&#"):
        prefix_length = 3 if entity[2:3] in {"x", "X"} else 2
        digits = entity[prefix_length:-1].lstrip("0")
        if len(digits) > 8:
            return "\ufffd"
        entity = f"{entity[:prefix_length]}{digits or '0'};"
    return html.unescape(entity)


def fits_telegram_html(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> bool:
    """Check raw and rendered code-point budgets and explicit formatting count."""
    if len(text) > max_length:
        return False
    if (
        text.count("<") > _MAX_FORMATTING_ENTITIES
        and sum(not match[1] for match in _TAG_RE.finditer(text)) > _MAX_FORMATTING_ENTITIES
    ):
        return False
    if len(text) <= MAX_MESSAGE_LENGTH:
        return True
    visible = re.sub(_ENTITY_PATTERN, lambda match: _decode_entity(match[0]), _TAG_RE.sub("", text))
    return len(visible) <= MAX_MESSAGE_LENGTH


def format_text(text: str, parse_mode: str = "HTML") -> tuple[str, str]:
    """
    Formats text for Telegram safely.

    Args:
        text (str): The input text (Markdown/CommonMark style from LLM).
        parse_mode (str): The desired parse mode. Defaults to 'HTML'.

    Returns:
        Tuple[str, str]: (formatted_text, parse_mode)
    """
    if not text:
        return "", parse_mode

    if parse_mode == "HTML":
        return markdown_to_html(text), "HTML"

    return text, parse_mode


def markdown_to_html(text: str) -> str:
    """
    Converts Markdown text to Telegram-supported HTML.
    Safely handles code blocks, inline code, bold, italic, and links.
    """
    if not text:
        return ""

    segments = _CODE_BLOCK_RE.split(text)

    html_parts = []

    for segment in segments:
        if segment.startswith("```") and segment.endswith("```") and len(segment) >= 6:
            inner = segment[3:-3]

            language = ""
            content = inner

            if "\n" in inner:
                first_line_end = inner.find("\n")
                possible_lang = inner[:first_line_end].strip()

                if possible_lang and len(possible_lang) < 20 and all(c.isalnum() or c in "+-#." for c in possible_lang):
                    language = possible_lang
                    content = inner[first_line_end + 1 :]
                else:
                    pass

            escaped_content = html.escape(content.strip())

            if not escaped_content:
                continue

            if language:
                html_parts.append(f'<pre><code class="language-{language}">{escaped_content}</code></pre>')
            else:
                html_parts.append(f"<pre>{escaped_content}</pre>")

        else:
            segment = _ESCAPE_CLEANUP_RE.sub(r"\1", segment)

            escaped_text = html.escape(segment)

            lines = escaped_text.split("\n")
            processed_lines: list[str] = []
            blockquote_buffer: list[str] = []

            def _flush_blockquote(
                blockquote_buffer: list[str] = blockquote_buffer,
                processed_lines: list[str] = processed_lines,
            ) -> None:
                """Flush accumulated blockquote lines into a single <blockquote> tag."""
                if blockquote_buffer:
                    inner = "\n".join(blockquote_buffer)
                    processed_lines.append(f"<blockquote>{inner}</blockquote>")
                    blockquote_buffer.clear()

            for line in lines:
                stripped = line.lstrip()

                if stripped.startswith("&gt; ") or stripped == "&gt;":
                    bq_content = stripped[5:] if stripped.startswith("&gt; ") else ""
                    blockquote_buffer.append(bq_content)
                    continue

                _flush_blockquote()

                heading_match = _HEADING_RE.match(stripped)
                if heading_match:
                    heading_text = heading_match.group(2).strip()
                    processed_lines.append(f"\n<b>{heading_text}</b>")
                    continue

                if _HR_RE.match(stripped):
                    processed_lines.append("━━━━━━━━━━━━━━━━")
                    continue

                processed_lines.append(line)

            _flush_blockquote()

            escaped_text = "\n".join(processed_lines)

            escaped_text = _INLINE_CODE_RE.sub(r"<code>\1</code>", escaped_text)

            escaped_text = _BOLD_RE.sub(r"<b>\1</b>", escaped_text)

            escaped_text = _ITALIC_UNDER_RE.sub(
                lambda match: match.group(0) if match.group(0) in _PYTHON_DUNDER_NAMES else f"<i>{match.group(1)}</i>",
                escaped_text,
            )

            escaped_text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", escaped_text)

            escaped_text = _ITALIC_SINGLE_UNDER_RE.sub(r"<i>\1</i>", escaped_text)

            escaped_text = _STRIKETHROUGH_RE.sub(r"<s>\1</s>", escaped_text)

            escaped_text = _LINK_RE.sub(r'<a href="\2">\1</a>', escaped_text)

            html_parts.append(escaped_text)

    return "".join(html_parts)


@dataclass(frozen=True, slots=True)
class _Markup:
    tags: tuple[tuple[str, str], ...] = ()
    closing: str = ""
    width: int = 0

    @classmethod
    def from_tags(cls, tags: tuple[tuple[str, str], ...]) -> _Markup:
        closing = "".join(f"</{name}>" for name, _ in reversed(tags))
        return cls(tags, closing, len(closing) + sum(len(opening) for _, opening in tags))

    def from_previous(self, previous: _Markup) -> tuple[str, int]:
        if self is previous:
            return "", 0
        shared = 0
        for old, new in zip(previous.tags, self.tags, strict=False):
            if old is not new:
                break
            shared += 1
        return (
            "".join(f"</{name}>" for name, _ in reversed(previous.tags[shared:]))
            + "".join(opening for _, opening in self.tags[shared:]),
            len(self.tags) - shared,
        )

    def fit(self, limit: int) -> _Markup:
        if self.width <= limit and len(self.tags) <= _MAX_FORMATTING_ENTITIES:
            return self
        tags = tuple(tag for tag in self.tags if len(tag[0]) + 3 + len(tag[1]) <= limit)[:_MAX_FORMATTING_ENTITIES]
        end = len(tags)
        width = sum(len(name) + 3 + len(opening) for name, opening in tags)
        while width > limit:
            end -= 1
            name, opening = tags[end]
            width -= len(opening) + len(name) + 3
        return _Markup.from_tags(tags[:end])


@dataclass(frozen=True, slots=True)
class _HtmlAtom:
    encoded: str
    visible_length: int
    whitespace: bool
    markup: _Markup


_NO_MARKUP = _Markup()
_ESCAPED_TEXT = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_NAMED_TELEGRAM_ENTITIES = frozenset({"&amp;", "&lt;", "&gt;", "&quot;"})


def _html_atoms(text: str, max_length: int) -> Iterator[_HtmlAtom]:
    tags: list[tuple[str, str]] = []
    markup = _NO_MARKUP
    markup_changed = False
    by_encoded_length: dict[int, _Markup] = {}

    def fitting_markup(encoded_length: int) -> _Markup:
        if encoded_length > max_length:
            raise ValueError("Telegram HTML budget cannot fit one escaped character")
        if encoded_length not in by_encoded_length:
            by_encoded_length[encoded_length] = markup.fit(max_length - encoded_length)
        return by_encoded_length[encoded_length]

    for match in _HTML_TOKEN_RE.finditer(text):
        token = match[0]
        if match[2]:
            name = match[2].lower()
            if not match[1]:
                tags.append((name, token))
            else:
                for index in range(len(tags) - 1, -1, -1):
                    if tags[index][0] == name:
                        del tags[index]
                        break
            markup_changed = True
            continue
        if markup_changed:
            markup = _Markup.from_tags(tuple(tags)) if tags else _NO_MARKUP
            markup_changed = False
            by_encoded_length.clear()
        if token.startswith("&") and token.endswith(";"):
            visible = _decode_entity(token)
            if visible and len(token) <= max_length and (token in _NAMED_TELEGRAM_ENTITIES or token.startswith("&#")):
                yield _HtmlAtom(token, len(visible), visible.isspace(), fitting_markup(len(token)))
                continue
            token = visible
        for char in token:
            encoded = _ESCAPED_TEXT.get(char, char)
            yield _HtmlAtom(encoded, 1, char.isspace(), fitting_markup(len(encoded)))


def split_text_safe(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split Telegram HTML while preserving text and the markup that fits around it.

    ``max_length`` bounds raw Python code points including reopened and closed
    tags. Telegram separately limits visible code points and formatting count.
    If the full formatting cannot fit, keep the visible text and fit as many
    of the surrounding tags as the budgets permit. Raise if even one escaped
    character cannot fit by itself.
    """
    if max_length < 1:
        raise ValueError("Telegram HTML chunks need a positive budget")
    if fits_telegram_html(text, max_length):
        return [text]

    chunks: list[str] = []
    atoms = iter(_html_atoms(text, max_length))
    pending: deque[_HtmlAtom] = deque()
    while (atom := pending.popleft() if pending else next(atoms, None)) is not None:
        pieces: list[str] = []
        chunk_atoms: list[_HtmlAtom] = []
        active = _NO_MARKUP
        raw_length = visible_length = formatting_count = 0
        preferred: tuple[int, str] | None = None

        while atom is not None:
            markup = atom.markup
            transition, opened_tags = markup.from_previous(active)
            piece = transition + atom.encoded
            proposed_length = raw_length + len(piece)
            if (
                proposed_length + len(markup.closing) > max_length
                or visible_length + atom.visible_length > MAX_MESSAGE_LENGTH
                or formatting_count + opened_tags > _MAX_FORMATTING_ENTITIES
            ):
                if preferred is not None:
                    cut, closing = preferred
                    pending.extendleft(reversed([*chunk_atoms[cut:], atom]))
                    chunks.append("".join(pieces[:cut]) + closing)
                else:
                    pending.appendleft(atom)
                    chunks.append("".join(pieces) + active.closing)
                break

            pieces.append(piece)
            chunk_atoms.append(atom)
            active = markup
            raw_length = proposed_length
            visible_length += atom.visible_length
            formatting_count += opened_tags
            if atom.whitespace and (
                raw_length + len(markup.closing) >= max_length // 2
                or visible_length >= MAX_MESSAGE_LENGTH // 2
                or formatting_count >= _MAX_FORMATTING_ENTITIES // 2
            ):
                preferred = len(pieces), markup.closing
            atom = pending.popleft() if pending else next(atoms, None)
        else:
            chunks.append("".join(pieces) + active.closing)

    return chunks


def sanitize_html_tags(html_text: str) -> str:
    """Ensure all HTML tags are properly balanced for Telegram.

    Designed for mid-stream flushes where markdown_to_html may produce
    unclosed or misnested tags from incomplete markdown
    (e.g. ``<code>...<i>text`` or ``<code>...<i>...</code>``).

    Uses a **rebuild approach**: walks through the HTML splitting it into
    text runs and tag tokens, maintaining a stack.  When a close tag
    doesn't match the stack top, the function inserts the necessary
    close/reopen tags *at that point* so that nesting is always valid.

    - Fixes misnested tags (e.g. ``<code><i>x</code>`` → ``<code><i>x</i></code>``)
    - Removes orphaned close tags (no matching open tag)
    - Appends missing close tags at the end in correct nesting order
    """
    if not html_text:
        return html_text

    open_stack: list[tuple[str, str]] = []
    result_parts: list[str] = []
    last_end = 0

    for match in _TAG_RE.finditer(html_text):
        result_parts.append(html_text[last_end : match.start()])
        last_end = match.end()

        is_close = match.group(1) == "/"
        tag_name = match.group(2).lower()
        full_tag = match.group(0)

        if not is_close:
            open_stack.append((tag_name, full_tag))
            result_parts.append(full_tag)
        else:
            if open_stack and open_stack[-1][0] == tag_name:
                open_stack.pop()
                result_parts.append(full_tag)
            elif any(name == tag_name for name, _ in open_stack):
                tags_to_reopen: list[tuple[str, str]] = []
                while open_stack and open_stack[-1][0] != tag_name:
                    inner_name, inner_open = open_stack.pop()
                    result_parts.append(f"</{inner_name}>")
                    tags_to_reopen.append((inner_name, inner_open))
                if open_stack:
                    open_stack.pop()
                result_parts.append(full_tag)
                for reopen_name, reopen_tag in reversed(tags_to_reopen):
                    result_parts.append(reopen_tag)
                    open_stack.append((reopen_name, reopen_tag))

    result_parts.append(html_text[last_end:])

    for tag_name, _ in reversed(open_stack):
        result_parts.append(f"</{tag_name}>")

    result = "".join(result_parts)

    while _EMPTY_TAG_RE.search(result):
        result = _EMPTY_TAG_RE.sub("", result)

    return result


def strip_formatting(text: str) -> str:
    """Removes all HTML tags and invisible characters."""
    text = _STRIP_HTML_RE.sub("", text)
    text = html.unescape(text)
    return text.strip()
