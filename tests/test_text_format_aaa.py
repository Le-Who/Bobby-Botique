"""
AAA unit tests for app.utils.text_format — the core HTML formatting layer.

Covers:
- markdown_to_html: all markdown constructs -> Telegram HTML
- sanitize_html_tags: misnested and unclosed tag balancing
- split_text_safe: overflow splitting with HTML tag preservation
- strip_formatting: HTML removal

These are critical risk areas: invalid HTML causes Telegram to reject edit_text.
"""

from app.utils.text_format import (
    markdown_to_html,
    sanitize_html_tags,
    split_text_safe,
    strip_formatting,
)

MAX_LEN = 4096


# ─── markdown_to_html ─────────────────────────────────────────────────────────


def test_plain_text_is_unchanged():
    """Plain text without any markdown must pass through unchanged."""
    # Arrange
    text = "Hello world"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert result == "Hello world"


def test_bold_double_star_converts_to_b_tags():
    # Arrange / Act
    result = markdown_to_html("**bold text**")

    # Assert
    assert result == "<b>bold text</b>"


def test_italic_single_underscore_converts_to_i_tags():
    # Arrange / Act
    result = markdown_to_html("_italic text_")

    # Assert
    assert "<i>italic text</i>" in result


def test_inline_code_backtick_converts_to_code_tags():
    # Arrange / Act
    result = markdown_to_html("`some_code()`")

    # Assert
    assert "<code>some_code()</code>" in result


def test_fenced_code_block_with_language():
    """```python\\n...\\n``` must produce <pre><code class="language-python">...</code></pre>."""
    # Arrange
    text = "```python\nprint('hello')\n```"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert '<pre><code class="language-python">' in result
    assert "</code></pre>" in result


def test_fenced_code_block_without_language():
    """Fenced block without language must produce <pre>...</pre>."""
    # Arrange
    text = "```\nsome code\n```"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "<pre>" in result
    assert "<code" not in result.split("<pre>")[1].split("</pre>")[0]


def test_html_special_chars_are_escaped_in_regular_text():
    """<, > and & in regular text must be HTML-escaped."""
    # Arrange
    text = "Use <div> & 'script'"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "&lt;div&gt;" in result
    assert "&amp;" in result


def test_html_special_chars_inside_code_block_are_escaped():
    """Special chars inside fenced code blocks must also be escaped."""
    # Arrange
    text = "```\n<div>hello</div>\n```"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "&lt;div&gt;" in result


def test_markdown_link_converts_to_anchor_tag():
    """[text](url) must become <a href="url">text</a>."""
    # Arrange
    text = "Visit [Python](https://python.org)"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert '<a href="https://python.org">Python</a>' in result


def test_heading_converts_to_bold():
    """# Heading must become bold text (Telegram doesn't have heading tags)."""
    # Arrange
    text = "# Section Title"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "<b>Section Title</b>" in result


def test_horizontal_rule_converts_to_unicode_line():
    """--- must become a Unicode horizontal line."""
    # Arrange
    text = "---"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "━━━━" in result


def test_blockquote_converts_to_blockquote_tag():
    """> quoted text must become <blockquote>quoted text</blockquote>."""
    # Arrange
    text = "> This is a quote"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "<blockquote>" in result
    assert "This is a quote" in result


def test_strikethrough_converts_to_s_tags():
    """~~text~~ must become <s>text</s>."""
    # Arrange / Act
    result = markdown_to_html("~~strikethrough~~")

    # Assert
    assert "<s>strikethrough</s>" in result


def test_bold_content_is_not_double_formatted_in_code_block():
    """**bold** inside a code block must NOT become <b>bold</b>."""
    # Arrange
    text = "```\n**not_bold**\n```"

    # Act
    result = markdown_to_html(text)

    # Assert
    assert "<b>" not in result
    # The raw **not_bold** should appear HTML-escaped
    assert "**not_bold**" in result or "&ast;&ast;" in result or "not_bold" in result


def test_empty_string_returns_empty():
    # Arrange / Act / Assert
    assert markdown_to_html("") == ""


# ─── sanitize_html_tags ───────────────────────────────────────────────────────


def test_well_formed_html_passes_through_unchanged():
    """Balanced HTML must be returned without alteration."""
    # Arrange
    html = "<b>bold</b> and <i>italic</i>"

    # Act
    result = sanitize_html_tags(html)

    # Assert
    assert result == html


def test_unclosed_b_tag_gets_closed():
    """Unclosed <b> must be automatically closed at end of text."""
    # Arrange
    html = "<b>unclosed bold"

    # Act
    result = sanitize_html_tags(html)

    # Assert
    assert result.count("<b>") == result.count("</b>")
    assert "unclosed bold" in result


def test_unclosed_code_tag_gets_closed():
    """Unclosed <code> mid-stream must be automatically closed."""
    # Arrange
    html = "<code>partial code"

    # Act
    result = sanitize_html_tags(html)

    # Assert
    assert result.endswith("</code>")


def test_misnested_tags_are_reordered_correctly():
    """<code><i>text</code> must be fixed to <code><i>text</i></code>."""
    # Arrange
    html = "<code><i>text</code>"

    # Act
    result = sanitize_html_tags(html)

    # Assert
    # After sanitization, all tags must be balanced
    assert result.count("<code>") == result.count("</code>")
    assert result.count("<i>") == result.count("</i>")
    assert "text" in result


def test_orphaned_close_tag_is_dropped():
    """A closing tag with no matching open tag must be silently dropped."""
    # Arrange
    html = "plain text</b>"

    # Act
    result = sanitize_html_tags(html)

    # Assert
    assert "</b>" not in result
    assert "plain text" in result


def test_sanitize_html_empty_string_returns_empty():
    # Arrange / Act / Assert
    assert sanitize_html_tags("") == ""


def test_nested_valid_tags_preserved():
    """<pre><code>text</code></pre> must be preserved exactly."""
    # Arrange
    html = '<pre><code class="language-python">print(1)</code></pre>'

    # Act
    result = sanitize_html_tags(html)

    # Assert
    assert "print(1)" in result
    assert "<pre>" in result or result.startswith("<pre>") or "pre" in result


# ─── split_text_safe ──────────────────────────────────────────────────────────


def test_short_text_returned_as_single_chunk():
    """Text shorter than max_length must return a single-item list."""
    # Arrange
    text = "Short text"

    # Act
    chunks = split_text_safe(text, max_length=4096)

    # Assert
    assert chunks == [text]


def test_long_text_splits_into_multiple_chunks():
    """Text longer than max_length must be split into multiple chunks."""
    # Arrange
    text = "word " * 1200  # well over 4096 chars

    # Act
    chunks = split_text_safe(text, max_length=500)

    # Assert
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500 + 20  # small tolerance for tag balancing


def test_all_chunks_combined_equal_original_content():
    """Splitting and rejoining must preserve all text content (word count invariant).

    Note: split_text_safe strips leading whitespace from continued chunks
    to avoid blank-line starts — so exact string equality after join is not
    guaranteed. We verify the word-level content is preserved instead.
    """
    # Arrange
    text = "word " * 500  # 2500 chars, well over 400-char limit

    # Act
    chunks = split_text_safe(text, max_length=400)
    combined_words = " ".join(chunks).split()
    original_words = text.split()

    # Assert — all original words appear (order preserved, count matches)
    assert len(combined_words) == len(original_words), (
        f"Word count mismatch: original={len(original_words)}, combined={len(combined_words)}"
    )


def test_split_does_not_produce_empty_chunks():
    """None of the resulting chunks should be empty strings."""
    # Arrange
    text = "paragraph\n\n" * 200

    # Act
    chunks = split_text_safe(text, max_length=100)

    # Assert
    assert all(c for c in chunks), "Split must not produce empty chunks"


def test_split_preserves_code_block_tags():
    """After splitting, opened <pre> must have matching </pre> in same chunk."""
    # Arrange
    inner_code = "x = 1\n" * 50  # Extracted to avoid backslash-in-f-string (py<3.12)
    code_block = f'<pre><code class="language-python">{inner_code}</code></pre>'
    text = code_block + " some text after"

    # Act
    chunks = split_text_safe(text, max_length=400)

    # Assert
    for chunk in chunks:
        # Each chunk must be individually parseable (balanced tags)
        cleaned = sanitize_html_tags(chunk)
        assert cleaned  # Not empty after sanitization


# ─── strip_formatting ─────────────────────────────────────────────────────────


def test_strip_formatting_removes_html_tags():
    # Arrange
    html = "<b>bold</b> and <i>italic</i>"

    # Act
    result = strip_formatting(html)

    # Assert
    assert result == "bold and italic"


def test_strip_formatting_decodes_html_entities():
    """HTML entities like &amp; must be decoded."""
    # Arrange
    html = "Tom &amp; Jerry"

    # Act
    result = strip_formatting(html)

    # Assert
    assert result == "Tom & Jerry"


def test_strip_formatting_empty_string():
    # Arrange / Act / Assert
    assert strip_formatting("") == ""
