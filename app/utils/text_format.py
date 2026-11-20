import html
import re

MAX_MESSAGE_LENGTH = 4096

_TAG_RE = re.compile(r"<(/?)(pre|code|b|i|a|u|s|em|strong|blockquote)(\s[^>]*)?>")
_EMPTY_TAG_RE = re.compile(r"<(pre|code|b|i|a|u|s|em|strong|blockquote)(?:\s[^>]*)?>(?:</\1>)")

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
_TAG_FIND_RE = re.compile(r"<(/?)(\w+)[^>]*>")
_STRIP_HTML_RE = re.compile(r"<[^>]+>")


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


def split_text_safe(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Splits text into chunks safe for Telegram, respecting HTML structure.
    Tries to split by newlines, then spaces, avoiding splitting inside tags.
    """
    if len(text) <= max_length:
        return [text]

    chunks = []
    for _guard in range(200):
        if not text:
            break
        if len(text) <= max_length:
            chunks.append(text)
            break

        limit = max_length
        cut_point = -1

        candidates = [
            text.rfind("</pre>", 0, limit),
            text.rfind("\n\n", 0, limit),
            text.rfind("\n", 0, limit),
            text.rfind(". ", 0, limit),
        ]

        valid_candidates = [c for c in candidates if c != -1]

        if valid_candidates:
            cut_point = max(valid_candidates)

            if cut_point == candidates[0]:
                cut_point += 6
            elif cut_point == candidates[1]:
                cut_point += 2
            elif cut_point == candidates[2]:
                cut_point += 1

        else:
            cut_point = text.rfind(" ", 0, limit)

        if cut_point == -1 or cut_point == 0:
            cut_point = limit

        last_open_angle = text.rfind("<", 0, cut_point)
        if last_open_angle != -1:
            last_close_angle_after_open = text.find(">", last_open_angle, cut_point)
            if last_close_angle_after_open == -1:  # noqa: SIM102
                if last_open_angle > 0:
                    cut_point = last_open_angle

        chunk = text[:cut_point]
        remaining = text[cut_point:].lstrip()

        open_tags = []
        tag_iter = _TAG_FIND_RE.finditer(chunk)
        for match in tag_iter:
            is_close = match.group(1) == "/"
            tag_name = match.group(2)
            full_match = match.group(0)

            if tag_name in ["br", "img", "hr"]:
                continue
            if not is_close:
                open_tags.append((tag_name, full_match))
            else:
                if open_tags and open_tags[-1][0] == tag_name:
                    open_tags.pop()

        closing_str = ""
        opening_str = ""
        for tag_name, full_match in reversed(open_tags):
            closing_str += f"</{tag_name}>"
            opening_str = full_match + opening_str
        chunk += closing_str
        if remaining:
            remaining = opening_str + remaining

        if cut_point <= 0 or remaining == text or len(remaining) >= len(text):
            cut_point = limit
            chunk = text[:cut_point]
            remaining = text[cut_point:].lstrip()
            chunks.append(chunk)
            text = remaining
            continue

        chunks.append(chunk)
        text = remaining

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
        tag_name = match.group(2)
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
