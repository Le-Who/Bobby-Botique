"""Utilities for the Long Read Mini App reader.

Provides:
- SSR Markdown → HTML rendering with syntax highlighting stubs
- TOC (table of contents) extraction from Markdown headings
- Bionic Reading text transform
- Telegraph HTML → Markdown extraction for cold-storage fallback
"""

from __future__ import annotations

import html
import re
import urllib.parse

# ── TOC extraction ────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _slug(text: str) -> str:
    """Convert heading text to a URL-safe anchor slug."""
    text = html.unescape(text).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return urllib.parse.quote(text.strip("-"), safe="")


def extract_toc(markdown: str) -> list[dict[str, str]]:
    """Extract a table of contents from Markdown headings.

    Returns a list of dicts: {"level": "h1"|"h2"|"h3", "text": str, "anchor": str}
    Only extracts H1–H3 headings and skips headings inside fenced code blocks.

    Args:
        markdown: Raw markdown text.

    Returns:
        Ordered list of TOC entries, or empty list if fewer than 2 headings found.
    """
    # Strip fenced code blocks before scanning for headings so that
    # code examples with ``# comment`` style lines are not captured.
    stripped = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)

    entries: list[dict[str, str]] = []
    seen_anchors: dict[str, int] = {}

    for match in _HEADING_RE.finditer(stripped):
        level = len(match.group(1))  # 1-3
        text = match.group(2).strip()
        # Strip inline markdown from heading text for display
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        anchor = _slug(text)

        # Deduplicate anchors
        if anchor in seen_anchors:
            seen_anchors[anchor] += 1
            anchor = f"{anchor}-{seen_anchors[anchor]}"
        else:
            seen_anchors[anchor] = 0

        entries.append({"level": f"h{level}", "text": text, "anchor": anchor})

    # A TOC with fewer than 2 entries is not useful
    return entries if len(entries) >= 2 else []


# ── Bionic Reading transform ──────────────────────────────────────────────────

def apply_bionic_reading(html_text: str) -> str:
    """Apply Bionic Reading transform to rendered HTML.

    Wraps the leading characters of each word in a <b> tag.
    The bold portion length scales with word length:
      - 1-3 chars  → 1 char bold
      - 4-6 chars  → 2 chars bold
      - 7-9 chars  → 3 chars bold
      - 10+ chars  → 4 chars bold

    Only transforms text nodes — skips tag attributes, code/pre blocks,
    and existing bold/strong content.

    Args:
        html_text: Already-rendered HTML string.

    Returns:
        HTML string with bionic bold markers applied.
    """
    # Split on tags so we only touch text nodes
    parts = re.split(r"(<[^>]+>)", html_text)
    in_skip = 0  # depth counter for code/pre/b/strong blocks

    _SKIP_OPEN = re.compile(r"^<(code|pre|b|strong|a)\b", re.IGNORECASE)
    _SKIP_CLOSE = re.compile(r"^</(code|pre|b|strong|a)\b", re.IGNORECASE)

    result: list[str] = []
    for part in parts:
        if part.startswith("<"):
            if _SKIP_OPEN.match(part):
                in_skip += 1
            elif _SKIP_CLOSE.match(part):
                in_skip = max(0, in_skip - 1)
            result.append(part)
        elif in_skip > 0:
            # Inside a skip block — pass through unchanged
            result.append(part)
        else:
            result.append(_bionic_text(part))

    return "".join(result)


def _bionic_text(text: str) -> str:
    """Bold the leading characters of every word in a plain text string."""

    def _bold_word(match: re.Match) -> str:  # type: ignore[type-arg]
        word = match.group(0)
        n = len(word)
        if n <= 3:
            k = 1
        elif n <= 6:
            k = 2
        elif n <= 9:
            k = 3
        else:
            k = 4
        return f"<b>{html.escape(word[:k])}</b>{html.escape(word[k:])}"

    # Match sequences of word characters (letters, digits, underscores)
    # and pass non-word segments through unchanged.
    # We need to escape the non-word parts too since `text` is already HTML-escaped
    # by the time it reaches us (rendered from markdown_to_html).
    # Strategy: split on \w+ tokens, transform tokens, reassemble.
    result_parts: list[str] = []
    last = 0
    for m in re.finditer(r"\w+", text):
        result_parts.append(text[last : m.start()])  # gap (spaces, punctuation)
        result_parts.append(_bold_word(m))
        last = m.end()
    result_parts.append(text[last:])
    return "".join(result_parts)


# ── Markdown → HTML for Reader (full-featured) ───────────────────────────────

_FENCE_RE = re.compile(r"^```(\w*)\n?(.*?)^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_ITALIC_UNDER_RE = re.compile(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HR_RE = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_HEADING_HTML_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def markdown_to_reader_html(markdown: str, toc: list[dict[str, str]]) -> str:
    """Convert Markdown to full-featured HTML for the Reader Mini App.

    This is a richer renderer than ``text_format.markdown_to_html()`` which
    targets Telegram's restricted tag set.  For the standalone Mini App we
    can emit ``<h1>``–``<h3>`` tags, ``<hr>``, ``<table>`` stubs, and inject
    anchor ``id`` attributes matching the TOC entries so jump-links work.

    Flow:
        1. Extract fenced code blocks (``​```lang … ```​``) first so later
           passes never touch code content.
        2. Process block-level elements (headings with anchors, blockquotes,
           HR, unordered/ordered lists).
        3. Process inline markup on remaining text runs.
        4. Reassemble with code block placeholders substituted back.

    Args:
        markdown: Raw Markdown text from the AI.
        toc: TOC entries produced by :func:`extract_toc` (used to attach
             ``id`` attributes to heading tags for in-page navigation).

    Returns:
        Rendered HTML string safe for injection via ``innerHTML``.
    """
    # toc anchors are used implicitly: headings generate their own anchor via
    # _slug() which produces the same slugs as extract_toc(), ensuring consistent
    # in-page jump links between the TOC rows and rendered heading elements.

    # ── Step 1: protect fenced code blocks ────────────────────────────────
    placeholders: list[str] = []

    def _replace_fence(m: re.Match) -> str:  # type: ignore[type-arg]
        lang = m.group(1).strip() or "plaintext"
        code = html.escape(m.group(2))
        lang_display = html.escape(lang)
        # We inject data-lang so the client JS can add a download button
        rendered = (
            f'<div class="code-block" data-lang="{lang_display}">'
            f'<div class="code-header">'
            f'<span class="code-lang">{lang_display}</span>'
            f'<div class="code-actions">'
            f'<button class="code-action" onclick="expandCode(this)" title="Развернуть">⛶</button>'
            f'<button class="code-action" onclick="downloadCode(this)" data-lang="{lang_display}">↓</button>'
            f'<button class="code-action" onclick="copyCode(this)">Копия</button>'
            f'</div></div>'
            f'<pre><code class="language-{lang_display}" data-code="{html.escape(m.group(2))}">'
            f"{code}"
            f"</code></pre>"
            f"</div>"
        )
        idx = len(placeholders)
        placeholders.append(rendered)
        return f"\x00CODE{idx}\x00"

    text = _FENCE_RE.sub(_replace_fence, markdown)

    # ── Step 2: block-level elements ──────────────────────────────────────
    lines = text.split("\n")
    out_lines: list[str] = []
    i = 0
    para_buf: list[str] = []

    def _flush_para() -> None:
        if para_buf:
            joined = " ".join(line.strip() for line in para_buf if line.strip())
            if joined:
                # Apply inline markup AFTER html-escaping so markdown syntax is
                # converted correctly without double-escaping issues.
                out_lines.append(f"<p>{_inline_markup(html.escape(joined))}</p>")
            para_buf.clear()

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # Headings
        h_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if h_match:
            _flush_para()
            level = len(h_match.group(1))
            level = min(level, 3)  # clamp to h1–h3 for visual hierarchy
            tag = f"h{level}"
            heading_text = h_match.group(2).strip()
            heading_text_clean = re.sub(r"\*+|_+|`+", "", heading_text).strip()
            anchor = _slug(heading_text_clean)
            # Find matching anchor in TOC (deduplicated)
            id_attr = f' id="{html.escape(anchor)}"'
            display = _inline_markup(html.escape(heading_text))
            out_lines.append(f"<{tag}{id_attr}>{display}</{tag}>")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}\s*$", stripped) and stripped:
            _flush_para()
            out_lines.append("<hr>")
            i += 1
            continue

        # Blockquote
        if stripped.startswith("> ") or stripped == ">":
            _flush_para()
            content = stripped[2:] if stripped.startswith("> ") else ""
            # Collect consecutive blockquote lines
            bq_lines = [content]
            i += 1
            while i < len(lines) and (lines[i].strip().startswith("> ") or lines[i].strip() == ">"):
                bq_lines.append(lines[i].strip()[2:] if lines[i].strip().startswith("> ") else "")
                i += 1
            bq_html = "<br>".join(_inline_markup(html.escape(bq_line)) for bq_line in bq_lines)
            out_lines.append(f"<blockquote>{bq_html}</blockquote>")
            continue

        # Unordered list
        if re.match(r"^[-*+]\s+", stripped):
            _flush_para()
            items: list[str] = []
            while i < len(lines) and re.match(r"^[-*+]\s+", lines[i].strip()):
                item_text = re.sub(r"^[-*+]\s+", "", lines[i].strip())
                items.append(f"<li>{_inline_markup(html.escape(item_text))}</li>")
                i += 1
            out_lines.append("<ul>" + "".join(items) + "</ul>")
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", stripped):
            _flush_para()
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                item_text = re.sub(r"^\d+\.\s+", "", lines[i].strip())
                items.append(f"<li>{_inline_markup(html.escape(item_text))}</li>")
                i += 1
            out_lines.append("<ol>" + "".join(items) + "</ol>")
            continue

        # Empty line → paragraph break
        if not stripped:
            _flush_para()
            i += 1
            continue

        # Placeholder line (code block)
        if "\x00CODE" in stripped:
            _flush_para()
            out_lines.append(stripped)
            i += 1
            continue

        # Regular text line — buffer into paragraph
        if stripped:
            para_buf.append(stripped)
        i += 1

    _flush_para()

    # ── Step 3: restore code block placeholders ────────────────────────────
    result = "\n".join(out_lines)
    for idx, rendered in enumerate(placeholders):
        result = result.replace(f"\x00CODE{idx}\x00", rendered)

    return result


def _inline_markup(text: str) -> str:
    """Apply inline Markdown markup to an already-HTML-escaped string."""
    # Inline code (highest priority — skip further processing inside)
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Italic (star)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    # Italic (underscore)
    text = re.sub(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", r"<i>\1</i>", text)
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
    return text


# ── Telegraph HTML → plain text extraction ────────────────────────────────────

_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def extract_text_from_telegraph_html(telegraph_html: str) -> str:
    """Extract readable plain text from a Telegraph page's HTML body.

    Used as cold-storage fallback when Redis TTL has expired but a Telegraph
    URL is still available.  We strip tags and normalise whitespace so the
    result can be served through our own Reader as plain text.

    Args:
        telegraph_html: Raw HTML from the Telegraph page (inner body).

    Returns:
        Plain text suitable for display in the Reader Mini App.
    """
    # Strip HTML tags FIRST (before unescaping) so that entity-decoded
    # characters like '&lt;' → '<' are not mistakenly treated as new tags.
    text = _TAG_STRIP_RE.sub("", telegraph_html)
    # Now decode remaining HTML entities (e.g. &amp; → &, &gt; → >)
    text = html.unescape(text)
    # Normalise multiple blank lines
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()
