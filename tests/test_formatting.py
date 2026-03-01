import unittest

from app.utils.formatting import (
    TelegramFormatter,
    escape_format_chars,
    format_key_for_display,
    strip_markdown,
)


class TestFormatting(unittest.TestCase):
    def test_format_text_basic(self):
        text = "Hello world"
        formatted, mode = TelegramFormatter.format_text(text)
        self.assertEqual(formatted, "Hello world")
        self.assertEqual(mode, "HTML")

    def test_format_text_bold(self):
        text = "Hello **bold** world"
        formatted, mode = TelegramFormatter.format_text(text)
        self.assertEqual(formatted, "Hello <b>bold</b> world")
        self.assertEqual(mode, "HTML")

    def test_format_text_italic(self):
        # Testing _italic_
        text = "Hello _italic_ world"
        formatted, mode = TelegramFormatter.format_text(text)
        self.assertEqual(formatted, "Hello <i>italic</i> world")
        self.assertEqual(mode, "HTML")

    def test_format_text_code(self):
        text = "Use `print()` function"
        formatted, mode = TelegramFormatter.format_text(text)
        self.assertEqual(formatted, "Use <code>print()</code> function")
        self.assertEqual(mode, "HTML")

    def test_format_text_link(self):
        text = "Click [here](http://example.com)"
        formatted, mode = TelegramFormatter.format_text(text)
        # The output order of attributes or quotes might vary, but let's assume standard behavior
        self.assertIn('href="http://example.com"', formatted)
        self.assertIn(">here</a>", formatted)
        self.assertEqual(mode, "HTML")

    def test_format_text_code_block(self):
        text = "```python\nprint('hello')\n```"
        formatted, mode = TelegramFormatter.format_text(text)
        # Expecting <pre><code class="language-python">...</code></pre>
        self.assertIn('<pre><code class="language-python">', formatted)
        # Quotes are escaped in HTML
        self.assertIn("print(&#x27;hello&#x27;)", formatted)

    def test_strip_markdown_html(self):
        # strip_markdown handles HTML tags stripping
        text = "Hello <b>bold</b> world"
        stripped = strip_markdown(text)
        self.assertEqual(stripped, "Hello bold world")

    def test_format_key_for_display(self):
        key = "1234567890abcdef"
        formatted = format_key_for_display(key)
        self.assertEqual(formatted, "12345...cdef")

        # Test invalid key
        self.assertEqual(format_key_for_display("short"), "Invalid Key")
        self.assertEqual(format_key_for_display(None), "Invalid Key")

    def test_escape_format_chars(self):
        text = "Hello {world}"
        escaped = escape_format_chars(text)
        self.assertEqual(escaped, "Hello {{world}}")

        text = "No braces"
        self.assertEqual(escape_format_chars(text), "No braces")

        self.assertEqual(escape_format_chars(None), None)


if __name__ == "__main__":
    unittest.main()
