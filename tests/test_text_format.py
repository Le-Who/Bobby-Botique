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
    assert "</code></pre>" in chunks[0] or ("</pre>" in chunks[0] and "</code>" in chunks[0])

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


def test_markdown_to_html_xss_prevention():
    # Regular safe links should pass
    assert format_text("[Safe link](https://example.com)")[0] == '<a href="https://example.com">Safe link</a>'
    assert format_text("[Safe link](tg://resolve?domain=test)")[0] == '<a href="tg://resolve?domain=test">Safe link</a>'

    # Dangerous schemes should be stripped
    assert format_text("[XSS link](javascript:alert('XSS'))")[0] == "XSS link"
    assert format_text("[VBScript link](vbscript:msgbox('XSS'))")[0] == "VBScript link"
    assert format_text("[Data URI](data:text/html;base64,PHNjcmlwdD5hbGVydCgnWFNTJyk8L3NjcmlwdD4=)")[0] == "Data URI"

    # Nested parentheses in the URL should work (up to one level)
    assert format_text("[Wiki link](https://en.wikipedia.org/wiki/Python_(programming_language))")[0] == '<a href="https://en.wikipedia.org/wiki/Python_(programming_language)">Wiki link</a>'

    # Obfuscated XSS with HTML entities
    assert format_text("[Obfuscated](&#x6a;avascript:alert(1))")[0] == "Obfuscated"
    assert format_text("[Obfuscated 2](&amp;#x6a;avascript:alert(1))")[0] == "Obfuscated 2"

    # Obfuscated XSS with control characters
    assert format_text("[Control Char](java\x00script:alert(1))")[0] == "Control Char"
    assert format_text("[Control Char 2](javascript\x09:alert(1))")[0] == "Control Char 2"


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
    assert format_text("[link](http://example.com)")[0] == '<a href="http://example.com">link</a>'


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
    The sanitizer must produce properly nested HTML, not just balanced counts.
    """
    html_input = "<code>some code <i>italic text</code>"
    result = sanitize_html_tags(html_input)
    # All tags should be balanced
    assert result.count("<code>") == result.count("</code>")
    assert result.count("<i>") == result.count("</i>")
    # Verify strict nesting via stack walk
    import re as _re

    stack = []
    for m in _re.finditer(r"<(/?)(pre|code|b|i|a|u|s|em|strong)(?:\s[^>]*)?>", result):
        is_close = m.group(1) == "/"
        tag = m.group(2)
        if not is_close:
            stack.append(tag)
        else:
            assert stack and stack[-1] == tag, f"Misnested: expected </{stack[-1]}>, got </{tag}> in: {result}"
            stack.pop()
    assert not stack, f"Unclosed tags {stack} in: {result}"


def test_sanitize_misnested_produces_valid_nesting():
    """Verify no close tag appears while a different tag is innermost.
    Uses a regex-based nesting validator.
    """
    html_input = "<code>E = <i>mc²</code> rest"
    result = sanitize_html_tags(html_input)

    # Walk the result and verify strict nesting
    import re as _re

    stack = []
    for m in _re.finditer(r"<(/?)(pre|code|b|i|a|u|s|em|strong)(?:\s[^>]*)?>", result):
        is_close = m.group(1) == "/"
        tag = m.group(2)
        if not is_close:
            stack.append(tag)
        else:
            assert stack, f"Orphaned </{tag}> in: {result}"
            assert stack[-1] == tag, f"Misnested: expected </{stack[-1]}>, got </{tag}> in: {result}"
            stack.pop()
    # After the close tags appended at end, stack should be empty
    assert not stack, f"Unclosed tags {stack} in: {result}"


def test_sanitize_reopen_after_misnested_close():
    """Tags closed to resolve misnesting should be re-opened if content follows."""
    html_input = "<b><i>text</b> more italic</i>"
    result = sanitize_html_tags(html_input)

    # Verify valid nesting
    import re as _re

    stack = []
    for m in _re.finditer(r"<(/?)(pre|code|b|i|a|u|s|em|strong)(?:\s[^>]*)?>", result):
        is_close = m.group(1) == "/"
        tag = m.group(2)
        if not is_close:
            stack.append(tag)
        else:
            assert stack and stack[-1] == tag, f"Bad nesting in: {result}"
            stack.pop()
    assert not stack, f"Unclosed tags {stack} in: {result}"
    # The word "more italic" should still be present
    assert "more italic" in result


def test_sanitize_empty_and_none():
    """Empty/falsy input passes through."""
    assert sanitize_html_tags("") == ""
    assert sanitize_html_tags(None) is None


def test_sanitize_plain_text():
    """Plain text without tags passes through unchanged."""
    assert sanitize_html_tags("hello world") == "hello world"


def test_sanitize_pre_code_unclosed():
    """Unclosed <pre><code> from incomplete streaming code block."""
    html_input = '<pre><code class="language-python">def hello():'
    result = sanitize_html_tags(html_input)
    assert result.endswith("</code></pre>")


def test_markdown_to_html_partial_streaming():
    """Simulate incomplete markdown during streaming: bold open, italic open."""
    # This is what happens mid-stream when AI sends "Hello **bold and *italic"
    text = "Hello **bold and *italic"
    result, _ = format_text(text)
    # After markdown_to_html, bold becomes <b> but italic * stays as text
    # The result may have unclosed tags — sanitize should fix them
    sanitized = sanitize_html_tags(result)

    # Verify valid nesting via stack walk
    import re as _re

    stack = []
    for m in _re.finditer(r"<(/?)(pre|code|b|i|a|u|s|em|strong|blockquote)(?:\s[^>]*)?>", sanitized):
        is_close = m.group(1) == "/"
        tag = m.group(2)
        if not is_close:
            stack.append(tag)
        else:
            assert stack and stack[-1] == tag, f"Bad nesting in: {sanitized}"
            stack.pop()
    assert not stack, f"Unclosed tags {stack} in: {sanitized}"


# ===================================================================
# Tests for headings, blockquotes, strikethrough (new features)
# ===================================================================


class TestMarkdownToHtmlHeadings:
    """Tests for Markdown heading → <b> conversion."""

    def test_h1_heading(self):
        result, _ = format_text("# Hello World")
        assert "<b>Hello World</b>" in result
        assert "#" not in result.replace("</", "")

    def test_h2_heading(self):
        result, _ = format_text("## Sub heading")
        assert "<b>Sub heading</b>" in result

    def test_h3_heading(self):
        result, _ = format_text("### Third level")
        assert "<b>Third level</b>" in result

    def test_heading_with_inline_formatting(self):
        result, _ = format_text("## **Bold heading**")
        # The heading itself becomes <b>, and ** also becomes <b>
        assert "<b>" in result
        assert "Bold heading" in result

    def test_heading_does_not_match_mid_line(self):
        result, _ = format_text("This is not # a heading")
        # Should NOT convert mid-line # to heading
        assert "<b>a heading</b>" not in result

    def test_multiple_headings(self):
        result, _ = format_text("# First\nSome text\n## Second")
        assert result.count("<b>") >= 2
        assert "First" in result
        assert "Second" in result


class TestMarkdownToHtmlBlockquotes:
    """Tests for Markdown blockquote → <blockquote> conversion."""

    def test_simple_blockquote(self):
        result, _ = format_text("> This is a quote")
        assert "<blockquote>" in result
        assert "This is a quote" in result
        assert "</blockquote>" in result

    def test_multiline_blockquote(self):
        result, _ = format_text("> Line 1\n> Line 2\n> Line 3")
        # Should be a single blockquote wrapping all lines
        assert result.count("<blockquote>") == 1
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_blockquote_then_normal(self):
        result, _ = format_text("> Quote\nNormal text")
        assert "<blockquote>" in result
        assert "Normal text" in result
        # Normal text should be outside the blockquote
        bq_end = result.index("</blockquote>")
        normal_pos = result.index("Normal text")
        assert normal_pos > bq_end

    def test_empty_blockquote_line(self):
        """A bare '>' with no text after it should not crash."""
        result, _ = format_text(">")
        assert "<blockquote>" in result


class TestMarkdownToHtmlStrikethrough:
    """Tests for ~~text~~ → <s>text</s> conversion."""

    def test_simple_strikethrough(self):
        result, _ = format_text("~~deleted~~")
        assert "<s>deleted</s>" in result

    def test_strikethrough_inline(self):
        result, _ = format_text("This is ~~old~~ new text")
        assert "<s>old</s>" in result
        assert "new text" in result

    def test_strikethrough_with_bold(self):
        result, _ = format_text("**bold** and ~~strike~~")
        assert "<b>bold</b>" in result
        assert "<s>strike</s>" in result


class TestMarkdownToHtmlHorizontalRules:
    """Tests for --- / *** / ___ → Unicode separator conversion."""

    def test_triple_dash(self):
        result, _ = format_text("---")
        assert "━━━" in result

    def test_triple_asterisk(self):
        result, _ = format_text("***")
        assert "━━━" in result

    def test_triple_underscore(self):
        result, _ = format_text("___")
        assert "━━━" in result

    def test_long_dash(self):
        result, _ = format_text("----------")
        assert "━━━" in result

    def test_hr_between_content(self):
        result, _ = format_text("Above\n---\nBelow")
        assert "Above" in result
        assert "━━━" in result
        assert "Below" in result

    def test_hr_not_matched_inside_text(self):
        """Dashes that are part of text should not become HR."""
        result, _ = format_text("some--text")
        assert "━━━" not in result
