import html
import re

import pytest

from app.utils.text_format import format_text, sanitize_html_tags, split_text_safe


@pytest.fixture
def mock_max_length():
    return 20


def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text)


def test_split_text_safe_no_split():
    text = "Hello world"
    chunks = split_text_safe(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_text_safe_simple_split(mock_max_length):
    # Text longer than mock_max_length
    text = "Hello world " * 5  # 60 chars
    chunks = split_text_safe(text, max_length=mock_max_length)

    assert len(chunks) > 1

    # Verify reconstruction (content check)
    reconstructed_content = "".join([strip_tags(c) for c in chunks])
    original_content = strip_tags(text)
    # Remove spaces for robust comparison as split logic might trim
    assert reconstructed_content.replace(" ", "") == original_content.replace(" ", "")

    for chunk in chunks:
        # Check length (approximate, since tags add length)
        # For plain text, it should respect max_length
        assert len(chunk) <= mock_max_length


def test_split_text_safe_newlines(mock_max_length):
    text = "Line 1\nLine 2\nLine 3\nLine 4"
    chunks = split_text_safe(text, max_length=mock_max_length)

    assert len(chunks) > 1
    # Should prefer splitting at newline
    # Expected: "Line 1\nLine 2\n" (14 chars) or "Line 1\n" (7 chars)
    # It should not split in the middle of "Line" unless forced
    assert "\n" in chunks[0]

    # Verify full content
    reconstructed = "".join(chunks)
    assert reconstructed == text


def test_split_text_safe_tag_balance(mock_max_length):
    text = "<b>Hello world this is a long bold text</b>"
    chunks = split_text_safe(text, max_length=mock_max_length)

    assert len(chunks) > 1

    # First chunk should have closing </b>
    assert chunks[0].endswith("</b>")
    # Second chunk should start with <b>
    assert chunks[1].startswith("<b>")

    # Verify content
    full_content = "".join([strip_tags(c) for c in chunks])
    # The original text has content "Hello world this is a long bold text"
    original_content = strip_tags(text)
    assert full_content.replace(" ", "") == original_content.replace(" ", "")


def test_split_text_safe_nested_tags(mock_max_length):
    text = "<b><i>Nested tags test here</i></b>"
    chunks = split_text_safe(text, max_length=mock_max_length)

    assert len(chunks) > 1

    # First chunk should close both
    # Note: Order of closing tags depends on stack popping.
    # Stack: [b, i]. Pop i -> </i>. Pop b -> </b>.
    assert chunks[0].endswith("</i></b>")

    # Second chunk should open both
    # Stack reconstruction: <b><i>
    assert chunks[1].startswith("<b><i>")

    full_content = "".join([strip_tags(c) for c in chunks])
    assert full_content.replace(" ", "") == strip_tags(text).replace(" ", "")


def test_split_text_safe_code_block():
    # Code block splitting
    code = 'def foo():\n    return "bar"\n'
    text = f'<pre><code class="python">{code}</code></pre>'

    # Force split
    # Length of start tag: <pre><code class="python"> is 25 chars.
    # Text is longer.
    chunks = split_text_safe(text, max_length=30)

    assert len(chunks) > 1

    # Check if tags are preserved in first chunk
    assert chunks[0].startswith("<pre><code")
    # It should close tags
    assert "</code></pre>" in chunks[0] or (
        "</pre>" in chunks[0] and "</code>" in chunks[0]
    )

    # Check subsequent chunks have context (reopened tags)
    # The implementation attempts to reconstruct tags.
    # We expect some form of reconstruction.
    found_reopen = False
    for chunk in chunks[1:]:
        if "<pre>" in chunk or "<code>" in chunk or "<code" in chunk:
            found_reopen = True
            break
    assert found_reopen, "Subsequent chunks should reopen code block tags"

    # Check content preservation
    content = "".join([strip_tags(c) for c in chunks])
    content_stripped = content.replace(" ", "")
    assert "deffoo" in content_stripped
    assert "return" in content_stripped


def test_split_text_safe_hard_split():
    text = "A" * 50
    chunks = split_text_safe(text, max_length=20)
    assert len(chunks) == 3
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 20
    assert len(chunks[2]) == 10


# --- New tests for format_text ---


def test_format_text_basic():
    text = "Hello world"
    result, mode = format_text(text)
    assert result == "Hello world"
    assert mode == "HTML"


def test_format_text_empty():
    result, mode = format_text("")
    assert result == ""
    assert mode == "HTML"


def test_format_text_non_html():
    text = "Hello world"
    result, mode = format_text(text, parse_mode="Markdown")
    assert result == text
    assert mode == "Markdown"


def test_markdown_to_html_formatting():
    # Bold
    assert format_text("**bold**")[0] == "<b>bold</b>"
    # Italic
    assert format_text("__italic__")[0] == "<i>italic</i>"
    assert format_text("*italic*")[0] == "<i>italic</i>"
    assert format_text("_italic_")[0] == "<i>italic</i>"
    # Inline code
    assert format_text("`code`")[0] == "<code>code</code>"
    # Link
    assert (
        format_text("[link](http://example.com)")[0]
        == '<a href="http://example.com">link</a>'
    )


def test_markdown_to_html_escaping():
    text = "<script>alert('xss')</script>"
    # html.escape escapes single quotes by default
    expected = "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
    assert format_text(text)[0] == expected


def test_markdown_to_html_code_block():
    code = 'def foo():\n    return "bar"'
    text = f"```python\n{code}\n```"
    result, _ = format_text(text)
    assert '<pre><code class="language-python">' in result
    assert html.escape(code) in result
    assert "</code></pre>" in result


def test_markdown_to_html_code_block_no_lang():
    code = "plain text code block"
    text = f"```\n{code}\n```"
    result, _ = format_text(text)
    assert "<pre>" in result
    assert html.escape(code) in result
    assert "</pre>" in result


def test_markdown_to_html_mixed():
    text = "Hello **bold** and `code`"
    result, _ = format_text(text)
    assert result == "Hello <b>bold</b> and <code>code</code>"


def test_markdown_to_html_nested():
    # Testing known behavior: bold first, then italic
    # **_bold italic_** -> <b>_bold italic_</b> -> <b><i>bold italic</i></b>
    text = "**_bold italic_**"
    result, _ = format_text(text)
    assert result == "<b><i>bold italic</i></b>"


# --- Tests for sanitize_html_tags (streaming HTML balancer) ---


def test_sanitize_already_balanced():
    """Balanced HTML passes through unchanged."""
    html = "<b>hello</b> <i>world</i>"
    assert sanitize_html_tags(html) == html


def test_sanitize_unclosed_bold():
    """Unclosed <b> tag gets closed at the end."""
    assert sanitize_html_tags("<b>hello") == "<b>hello</b>"


def test_sanitize_unclosed_nested():
    """Multiple unclosed tags closed in correct nesting order."""
    result = sanitize_html_tags("<b><i>text")
    assert result == "<b><i>text</i></b>"


def test_sanitize_code_italic_mismatch():
    """Reproduces the exact error from production logs:
    <code>...<i>text</code> where <i> crosses <code> boundary.
    The sanitizer should ensure all tags are balanced.
    """
    html = "<code>some code <i>italic text</code>"
    result = sanitize_html_tags(html)
    # <i> should be closed (misnesting resolved by appending </i>)
    assert "</i>" in result
    # All tags should be balanced
    assert result.count("<code>") == result.count("</code>")
    assert result.count("<i>") == result.count("</i>")


def test_sanitize_empty_and_none():
    """Empty/falsy input passes through."""
    assert sanitize_html_tags("") == ""
    assert sanitize_html_tags(None) is None


def test_sanitize_plain_text():
    """Plain text without tags passes through unchanged."""
    assert sanitize_html_tags("hello world") == "hello world"


def test_sanitize_pre_code_unclosed():
    """Unclosed <pre><code> from incomplete streaming code block."""
    html = '<pre><code class="language-python">def hello():'
    result = sanitize_html_tags(html)
    assert result.endswith("</code></pre>")


def test_markdown_to_html_partial_streaming():
    """Simulate incomplete markdown during streaming: bold open, italic open."""
    # This is what happens mid-stream when AI sends "Hello **bold and *italic"
    text = "Hello **bold and *italic"
    result, _ = format_text(text)
    # After markdown_to_html, bold becomes <b> but italic * stays as text
    # The result may have unclosed tags — sanitize should fix them
    sanitized = sanitize_html_tags(result)
    # Count open and close tags
    open_count = sanitized.count("<b>") + sanitized.count("<i>") + sanitized.count("<code>")
    close_count = sanitized.count("</b>") + sanitized.count("</i>") + sanitized.count("</code>")
    assert open_count == close_count, f"Unbalanced tags in: {sanitized}"

