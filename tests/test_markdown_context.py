"""Tests for _detect_open_markdown — markdown context detection for streaming overflow."""

import pytest

from app.streaming import _detect_open_markdown


class TestFencedCodeBlocks:
    """Fenced code block detection (``` ... ```)."""

    def test_no_code_block(self):
        suffix, prefix = _detect_open_markdown("Hello **bold** world")
        assert "```" not in suffix
        assert "```" not in prefix

    def test_open_code_block_no_lang(self):
        text = "Some text\n```\nprint('hello')\n"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == "\n```"
        assert prefix == "```\n"

    def test_open_code_block_with_lang(self):
        text = "Before code:\n```python\ndef foo():\n    return 42\n"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == "\n```"
        assert prefix == "```python\n"

    def test_closed_code_block(self):
        text = "```python\ndef foo():\n    return 42\n```\nAfter code."
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == ""
        assert prefix == ""

    def test_two_closed_code_blocks(self):
        text = "```js\nvar x;\n```\n\n```python\npass\n```"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == ""
        assert prefix == ""

    def test_three_fences_means_second_block_open(self):
        text = "```python\nprint(1)\n```\nText\n```ruby\nputs 'hi'\n"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == "\n```"
        assert prefix == "```ruby\n"

    def test_code_block_suppresses_inline_formatting(self):
        """Inside a code block, ** and * should not be counted as formatting."""
        text = "```\n**not bold** *not italic*\n"
        suffix, prefix = _detect_open_markdown(text)
        # Only code block context, not bold/italic
        assert suffix == "\n```"
        assert prefix == "```\n"


class TestInlineCode:
    """Inline code detection (` ... `)."""

    def test_no_inline_code(self):
        suffix, prefix = _detect_open_markdown("Hello world")
        assert suffix == ""

    def test_open_inline_code(self):
        text = "Use `print("
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == "`"
        assert prefix == "`"

    def test_closed_inline_code(self):
        text = "Use `print()` for output"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == ""
        assert prefix == ""

    def test_multiple_closed_inline_codes(self):
        text = "Both `foo` and `bar` work"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == ""

    def test_inline_code_suppresses_formatting(self):
        """Open inline code should return early without checking bold/italic."""
        text = "Check `some_code **inside"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == "`"
        assert prefix == "`"


class TestBold:
    """Bold detection (** ... **)."""

    def test_open_bold(self):
        text = "Hello **bold text"
        suffix, prefix = _detect_open_markdown(text)
        assert "**" in suffix
        assert "**" in prefix

    def test_closed_bold(self):
        text = "Hello **bold** text"
        suffix, prefix = _detect_open_markdown(text)
        assert "**" not in suffix

    def test_multiple_bold_one_open(self):
        text = "**closed** then **still open"
        suffix, prefix = _detect_open_markdown(text)
        assert "**" in suffix
        assert "**" in prefix


class TestItalic:
    """Italic detection (* ... *)."""

    def test_open_italic_star(self):
        text = "Hello *italic text"
        suffix, prefix = _detect_open_markdown(text)
        assert "*" in suffix
        assert "*" in prefix

    def test_closed_italic_star(self):
        text = "Hello *italic* text"
        suffix, prefix = _detect_open_markdown(text)
        assert suffix == ""

    def test_bold_and_italic_open(self):
        text = "**bold *italic"
        suffix, prefix = _detect_open_markdown(text)
        assert "**" in suffix
        assert "*" in suffix


class TestCombinations:
    """Edge cases and combinations."""

    def test_empty_text(self):
        suffix, prefix = _detect_open_markdown("")
        assert suffix == ""
        assert prefix == ""

    def test_plain_text(self):
        suffix, prefix = _detect_open_markdown("Just some plain text without formatting")
        assert suffix == ""
        assert prefix == ""

    def test_code_block_takes_priority(self):
        """Open code block should take priority and return early."""
        text = "**bold then\n```python\ncode"
        suffix, prefix = _detect_open_markdown(text)
        # Code block is open → only code block context, ignoring bold
        assert suffix == "\n```"
        assert "**" not in suffix

    def test_symmetry(self):
        """suffix and prefix should use matching markers."""
        text = "**open bold *open italic"
        suffix, prefix = _detect_open_markdown(text)
        # Both should contain ** and *
        assert suffix.count("**") == prefix.count("**")
        assert suffix.count("*") == prefix.count("*")
