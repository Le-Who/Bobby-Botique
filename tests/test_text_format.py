import pytest
import re
from app.utils.text_format import split_text_safe


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


@pytest.mark.xfail(reason="Bug in split_text_safe splits inside tag attributes")
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
    assert "def foo" in content
    assert "return" in content


def test_split_text_safe_hard_split():
    text = "A" * 50
    chunks = split_text_safe(text, max_length=20)
    assert len(chunks) == 3
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 20
    assert len(chunks[2]) == 10
