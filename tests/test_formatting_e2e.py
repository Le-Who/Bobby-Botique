"""End-to-end tests for the full text formatting pipeline.

Tests the complete flow: raw Markdown → markdown_to_html() → sanitize_html_tags()
→ split_text_safe(), verifying that the output is always valid Telegram HTML.
"""

import html
import re

import pytest

from app.utils.text_format import (
    format_text,
    markdown_to_html,
    sanitize_html_tags,
    split_text_safe,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


# Tags that Telegram supports (and our pipeline emits)
_TELEGRAM_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}


def _validate_balanced_html(text: str) -> None:
    """Assert every opened tag is properly closed and nested."""
    tag_pattern = re.compile(r"<(/?)(\w+)(?:\s[^>]*)?>")
    stack: list[str] = []
    for match in tag_pattern.finditer(text):
        is_closing, tag_name = match.group(1) == "/", match.group(2)
        assert tag_name in _TELEGRAM_TAGS, f"Unsupported Telegram HTML tag: {tag_name}"
        if is_closing:
            assert stack, f"Closing </{tag_name}> without matching open tag in: {text!r}"
            assert stack[-1] == tag_name, f"Tag mismatch: expected </{stack[-1]}>, got </{tag_name}> in: {text!r}"
            stack.pop()
        else:
            stack.append(tag_name)
    assert not stack, f"Unclosed tags {stack} in: {text!r}"


def _full_pipeline(md: str) -> str:
    """Run text through the complete pipeline and return sanitized HTML."""
    html_text, pm = format_text(md)
    assert pm == "HTML"
    return sanitize_html_tags(html_text)


# ── Test: Combined formatting ────────────────────────────────────────────────


class TestCombinedFormatting:
    """Complex text with multiple formatting types mixed together."""

    def test_heading_bold_italic_code(self):
        md = "# Heading\n\n**bold** and *italic* and `code`"
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        assert "<b>" in result  # heading or bold
        assert "<i>" in result
        assert "<code>" in result

    def test_blockquote_with_bold(self):
        md = "> This is a **quoted** line\n> And *another*"
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        assert "<blockquote>" in result
        assert "<b>quoted</b>" in result
        assert "<i>another</i>" in result

    def test_strikethrough_and_bold_mixed(self):
        md = "~~deleted~~ and **kept** and ~~also removed~~"
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        assert "<s>deleted</s>" in result
        assert "<b>kept</b>" in result

    def test_code_block_with_surrounding_formatting(self):
        md = "**Before code:**\n```python\ndef hello():\n    pass\n```\n*After code.*"
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        assert "<pre>" in result
        assert "<i>After code.</i>" in result

    def test_heading_blockquote_code_list_combined(self):
        md = (
            "# Title\n\n"
            "> Important note\n\n"
            "- item **one**\n"
            "- item *two*\n"
            "- `code item`\n\n"
            "## Sub-heading\n\n"
            "~~old info~~ → new info"
        )
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        # Must contain all format types
        assert "<b>" in result
        assert "<blockquote>" in result
        assert "<code>" in result
        assert "<s>" in result

    def test_link_with_bold_and_italic(self):
        md = "Check **[Google](https://google.com)** for *details*."
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        assert "https://google.com" in result


# ── Test: Sanitization round-trip ────────────────────────────────────────────


class TestSanitizationRoundTrip:
    """format_text → sanitize_html_tags always produces valid HTML."""

    @pytest.mark.parametrize(
        "md",
        [
            "**bold**",
            "*italic*",
            "~~strike~~",
            "`code`",
            "```\nblock\n```",
            "> quote",
            "# heading",
            "[link](http://example.com)",
            "plain text with no formatting",
            "",
        ],
        ids=[
            "bold",
            "italic",
            "strike",
            "code",
            "codeblock",
            "quote",
            "heading",
            "link",
            "plain",
            "empty",
        ],
    )
    def test_individual_format_round_trip(self, md):
        result = _full_pipeline(md)
        _validate_balanced_html(result)

    def test_sanitize_truncated_bold(self):
        """Simulates mid-stream truncation inside a bold tag."""
        partial_html = "<b>bold text without closing"
        result = sanitize_html_tags(partial_html)
        _validate_balanced_html(result)

    def test_sanitize_truncated_nested(self):
        """Mid-stream truncation with nested tags."""
        partial_html = "<b>bold and <i>italic without"
        result = sanitize_html_tags(partial_html)
        _validate_balanced_html(result)
        assert "</i>" in result
        assert "</b>" in result

    def test_sanitize_removes_empty_tags(self):
        """Empty tag pairs should be cleaned up."""
        html_text = "<b></b> some text <i></i>"
        result = sanitize_html_tags(html_text)
        assert "<b></b>" not in result
        assert "<i></i>" not in result


# ── Test: Split + rebalance ──────────────────────────────────────────────────


class TestSplitAndRebalance:
    """Long text is split correctly, each chunk is valid HTML."""

    def test_long_formatted_text_splits_with_valid_chunks(self):
        # Generate text > 4096 chars using repeated formatted blocks
        block = "**Bold** *italic* `code` ~~strike~~ plain text. " * 20
        chunks = split_text_safe(block, max_length=200)
        assert len(chunks) > 1
        for chunk in chunks:
            _validate_balanced_html(chunk)

    def test_split_preserves_content(self):
        """Content is not lost during splitting."""
        md = "Word " * 1000  # ~5000 chars
        html_text = markdown_to_html(md)
        chunks = split_text_safe(html_text, max_length=500)
        reconstructed = "".join(re.sub(r"<[^>]+>", "", c) for c in chunks)
        original_clean = re.sub(r"<[^>]+>", "", html_text)
        assert reconstructed.replace(" ", "") == original_clean.replace(" ", "")

    def test_split_code_block_survives(self):
        """Code blocks should not be broken in the middle."""
        md = "Before\n```python\nfor i in range(100):\n    print(i)\n```\nAfter text " * 5
        html_text = markdown_to_html(md)
        chunks = split_text_safe(html_text, max_length=200)
        for chunk in chunks:
            _validate_balanced_html(chunk)


# ── Test: Snake_case safety ──────────────────────────────────────────────────


class TestSnakeCaseSafety:
    """Underscores in snake_case identifiers should NOT become italic."""

    @pytest.mark.parametrize(
        "text",
        [
            "my_variable_name is good",
            "use get_user_by_id() to fetch",
            "UPPER_CASE_CONST = 42",
            "__init__ method",
            "__init__() constructs an instance",
            "__name__ and __file__ describe the module",
            "__repr__ returns text from __dict__",
            "path/to/file_name.py",
        ],
        ids=["simple", "function", "upper", "dunder", "dunder-call", "module-dunders", "object-dunders", "path"],
    )
    def test_snake_case_not_italicized(self, text):
        result = _full_pipeline(text)
        _validate_balanced_html(result)
        assert "<i>" not in result
        assert result == text

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("This is _italic text_ here", "This is <i>italic text</i> here"),
            ("__italic__", "<i>italic</i>"),
            ("*italic*", "<i>italic</i>"),
            ("__init__ with __italic__", "__init__ with <i>italic</i>"),
            ("__italic__ then __init__", "<i>italic</i> then __init__"),
            ("__init__ with _italic_", "__init__ with <i>italic</i>"),
            ("__init__ with *italic*", "__init__ with <i>italic</i>"),
        ],
        ids=[
            "underscore",
            "double-underscore",
            "star",
            "dunder-before",
            "dunder-after",
            "dunder-single",
            "dunder-star",
        ],
    )
    def test_real_italic_still_works(self, text, expected):
        result = _full_pipeline(text)
        _validate_balanced_html(result)
        assert result == expected


# ── Test: Special characters ─────────────────────────────────────────────────


class TestSpecialCharacters:
    """HTML-sensitive characters are escaped properly."""

    @pytest.mark.parametrize(
        "text",
        [
            "2 < 3 and 5 > 4",
            "Tom & Jerry",
            'She said "hello"',
            "<script>alert('xss')</script>",
            "Price: $5.00 (10% off!)",
            "C:\\Users\\admin\\Desktop",
            "Формула: x² + y² = z²",
        ],
        ids=[
            "lt_gt",
            "ampersand",
            "quotes",
            "xss",
            "special_chars",
            "backslash",
            "unicode",
        ],
    )
    def test_special_chars_safe(self, text):
        result = _full_pipeline(text)
        _validate_balanced_html(result)
        # These inputs are plain text, including the literal script-tag example.
        assert result == html.escape(text)

    def test_ampersand_escaped(self):
        result = _full_pipeline("Tom & Jerry")
        assert "&amp;" in result


# ── Test: Streaming context preservation ─────────────────────────────────────


# ── Test: Edge cases / stress ────────────────────────────────────────────────


class TestEdgeCases:
    """Unusual inputs that might break the pipeline."""

    def test_empty_string(self):
        result = _full_pipeline("")
        assert result == ""

    def test_only_whitespace(self):
        result = _full_pipeline("   \n\n   ")
        _validate_balanced_html(result)

    def test_very_long_single_word(self):
        word = "a" * 5000
        result = _full_pipeline(word)
        _validate_balanced_html(result)
        assert "a" * 100 in result  # content preserved

    def test_many_nested_formatting_markers(self):
        md = "**bold *italic ~~strike `code` strike~~ italic* bold**"
        result = _full_pipeline(md)
        _validate_balanced_html(result)

    def test_unmatched_markers_dont_crash(self):
        """Odd number of markers should not crash."""
        texts = [
            "*** broken",
            "~~ unclosed",
            "_ also broken ___ more",
            "``` no closing",
        ]
        for text in texts:
            result = _full_pipeline(text)
            _validate_balanced_html(result)

    def test_repeated_format_markers(self):
        md = "****empty bold**** and ~~~~empty strike~~~~"
        result = _full_pipeline(md)
        _validate_balanced_html(result)

    def test_mixed_cyrillic_and_latin_formatting(self):
        md = "**Жирный текст** и *курсив* с `кодом` и ~~зачёркнутый~~"
        result = _full_pipeline(md)
        _validate_balanced_html(result)
        assert "<b>" in result
        assert "<i>" in result
        assert "<s>" in result
        assert "<code>" in result
