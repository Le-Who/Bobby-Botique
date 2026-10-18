"""
Tests for app.utils.formatting — TelegramFormatter and format_key_for_display.

Rewritten from unittest.TestCase to pure pytest (AAA).
Each test covers exactly one observable behavior.
"""

from app.utils.formatting import (
    TelegramFormatter,
    format_key_for_display,
)

# ─── TelegramFormatter.format_text ───────────────────────────────────────────


def test_plain_text_is_returned_unchanged():
    """Plain text with no markdown must pass through as-is."""
    # Arrange
    text = "Hello world"

    # Act
    formatted, mode = TelegramFormatter.format_text(text)

    # Assert
    assert formatted == "Hello world"
    assert mode == "HTML"


def test_bold_markdown_converts_to_html_b_tags():
    """**bold** must be converted to <b>bold</b>."""
    # Arrange
    text = "Hello **bold** world"

    # Act
    formatted, mode = TelegramFormatter.format_text(text)

    # Assert
    assert "<b>bold</b>" in formatted
    assert mode == "HTML"


def test_italic_underscore_converts_to_html_i_tags():
    """_italic_ must be converted to <i>italic</i>."""
    # Arrange
    text = "Hello _italic_ world"

    # Act
    formatted, mode = TelegramFormatter.format_text(text)

    # Assert
    assert "<i>italic</i>" in formatted


def test_inline_code_converts_to_html_code_tags():
    """`print()` must be converted to <code>print()</code>."""
    # Arrange
    text = "Use `print()` function"

    # Act
    formatted, mode = TelegramFormatter.format_text(text)

    # Assert
    assert "<code>print()</code>" in formatted
    assert mode == "HTML"


def test_markdown_link_converts_to_html_anchor():
    """[text](url) must become <a href="url">text</a>."""
    # Arrange
    text = "Click [here](http://example.com)"

    # Act
    formatted, mode = TelegramFormatter.format_text(text)

    # Assert
    assert 'href="http://example.com"' in formatted
    assert ">here</a>" in formatted


def test_fenced_code_block_with_language_converts_to_pre_code():
    """```python\\ncode\\n``` must produce <pre><code class="language-python">…</code></pre>."""
    # Arrange
    text = "```python\nprint('hello')\n```"

    # Act
    formatted, mode = TelegramFormatter.format_text(text)

    # Assert
    assert '<pre><code class="language-python">' in formatted


def test_empty_string_returns_empty_string():
    """Empty input must produce empty HTML output."""
    # Arrange / Act
    formatted, mode = TelegramFormatter.format_text("")

    # Assert
    assert formatted == ""


def test_preserve_formatting_false_strips_all_tags():
    """When preserve_formatting=False, all markup must be stripped."""
    # Arrange
    text = "Hello **bold** world"

    # Act
    formatted, mode = TelegramFormatter.format_text(text, preserve_formatting=False)

    # Assert
    assert "<b>" not in formatted
    assert mode is None


# ─── format_key_for_display ──────────────────────────────────────────────────


def test_format_key_for_display_masks_middle_chars():
    """API key must be displayed as 'start...end'."""
    # Arrange
    key = "1234567890abcdef"

    # Act
    result = format_key_for_display(key)

    # Assert
    assert result == "12345...cdef"


def test_format_key_for_display_too_short_returns_invalid():
    """Keys shorter than 10 characters must return 'Invalid Key'."""
    # Arrange
    key = "short"

    # Act
    result = format_key_for_display(key)

    # Assert
    assert result == "Invalid Key"


def test_format_key_for_display_none_returns_invalid():
    """None input must return 'Invalid Key'."""
    # Arrange / Act
    result = format_key_for_display(None)

    # Assert
    assert result == "Invalid Key"
