"""
Tests for app.utils.formatting — TelegramFormatter, escape_format_chars,
format_key_for_display, strip_markdown.

Rewritten from unittest.TestCase to pure pytest (AAA).
Each test covers exactly one observable behavior.
"""


from app.utils.formatting import (
    TelegramFormatter,
    escape_format_chars,
    format_key_for_display,
    strip_markdown,
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


# ─── strip_markdown ───────────────────────────────────────────────────────────


def test_strip_markdown_removes_html_tags_leaving_text():
    """HTML tags must be stripped, leaving only the text content."""
    # Arrange
    text = "Hello <b>bold</b> world"

    # Act
    result = strip_markdown(text)

    # Assert
    assert result == "Hello bold world"


def test_strip_markdown_empty_string_returns_empty():
    # Arrange / Act / Assert
    assert strip_markdown("") == ""


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


# ─── escape_format_chars ─────────────────────────────────────────────────────


def test_escape_format_chars_doubles_curly_braces():
    """Curly braces must be doubled to prevent str.format() expansion."""
    # Arrange
    text = "Hello {world}"

    # Act
    result = escape_format_chars(text)

    # Assert
    assert result == "Hello {{world}}"


def test_escape_format_chars_no_braces_returns_unchanged():
    """Text without braces must be returned unchanged."""
    # Arrange
    text = "No braces"

    # Act
    result = escape_format_chars(text)

    # Assert
    assert result == "No braces"


def test_escape_format_chars_none_returns_none():
    """None input must be returned as-is (falsy pass-through)."""
    # Arrange / Act / Assert
    assert escape_format_chars(None) is None
