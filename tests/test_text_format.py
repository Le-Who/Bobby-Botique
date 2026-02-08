import unittest
from app.utils.text_format import markdown_to_html

class TestMarkdownToHtml(unittest.TestCase):
    def test_empty_input(self):
        """Test with empty string and None."""
        self.assertEqual(markdown_to_html(""), "")
        self.assertEqual(markdown_to_html(None), "")

    def test_plain_text(self):
        """Test plain text without markdown."""
        text = "Hello world"
        self.assertEqual(markdown_to_html(text), "Hello world")

    def test_html_escaping(self):
        """Test escaping of HTML special characters."""
        text = "Hello <world> & goodbye"
        expected = "Hello &lt;world&gt; &amp; goodbye"
        self.assertEqual(markdown_to_html(text), expected)

    def test_bold(self):
        """Test bold formatting."""
        text = "**bold** text"
        expected = "<b>bold</b> text"
        self.assertEqual(markdown_to_html(text), expected)

        # Test with multiple bold sections
        text = "**bold1** and **bold2**"
        expected = "<b>bold1</b> and <b>bold2</b>"
        self.assertEqual(markdown_to_html(text), expected)

    def test_italic_underscore_double(self):
        """Test italic formatting with double underscore."""
        text = "__italic__ text"
        expected = "<i>italic</i> text"
        self.assertEqual(markdown_to_html(text), expected)

    def test_italic_asterisk(self):
        """Test italic formatting with single asterisk."""
        text = "*italic* text"
        expected = "<i>italic</i> text"
        self.assertEqual(markdown_to_html(text), expected)

        # Ensure it doesn't match inside bold
        text = "**bold** *italic*"
        expected = "<b>bold</b> <i>italic</i>"
        self.assertEqual(markdown_to_html(text), expected)

    def test_italic_underscore_single(self):
        """Test italic formatting with single underscore."""
        text = "_italic_ text"
        expected = "<i>italic</i> text"
        self.assertEqual(markdown_to_html(text), expected)

        # Should not match inside words (snake_case)
        text = "snake_case_text"
        self.assertEqual(markdown_to_html(text), "snake_case_text")

    def test_inline_code(self):
        """Test inline code formatting."""
        text = "`code` text"
        expected = "<code>code</code> text"
        self.assertEqual(markdown_to_html(text), expected)

        # Test escaping inside inline code
        text = "`<script>`"
        expected = "<code>&lt;script&gt;</code>"
        self.assertEqual(markdown_to_html(text), expected)

    def test_code_block_with_language(self):
        """Test code block with language specifier."""
        text = "```python\nprint('hello')\n```"
        expected = '<pre><code class="language-python">print(&#x27;hello&#x27;)</code></pre>'
        self.assertEqual(markdown_to_html(text), expected)

    def test_code_block_without_language(self):
        """Test code block without language specifier."""
        text = "```\nprint('hello')\n```"
        # Depending on implementation, might default to no class or language-
        # Looking at code: if language is empty, it uses <pre>content</pre>
        expected = "<pre>print(&#x27;hello&#x27;)</pre>"
        self.assertEqual(markdown_to_html(text), expected)

    def test_code_block_escaping(self):
        """Test escaping inside code blocks."""
        text = "```html\n<div>content</div>\n```"
        expected = '<pre><code class="language-html">&lt;div&gt;content&lt;/div&gt;</code></pre>'
        self.assertEqual(markdown_to_html(text), expected)

    def test_links(self):
        """Test link formatting."""
        text = "[Google](https://google.com)"
        expected = '<a href="https://google.com">Google</a>'
        self.assertEqual(markdown_to_html(text), expected)

    def test_mixed_formatting(self):
        """Test mixed formatting."""
        text = "**Bold** and *Italic* and `Code`"
        expected = "<b>Bold</b> and <i>Italic</i> and <code>Code</code>"
        self.assertEqual(markdown_to_html(text), expected)

    def test_nested_formatting_bold_italic(self):
        """Test nested formatting (Bold containing Italic)."""
        # Note: The implementation uses sequential regex replacement.
        # **_italic_** -> <b>_italic_</b> -> <b><i>italic</i></b>
        text = "**_italic_**"
        expected = "<b><i>italic</i></b>"
        self.assertEqual(markdown_to_html(text), expected)

    def test_malicious_input(self):
        """Test handling of malicious HTML input."""
        text = "<script>alert('xss')</script>"
        expected = "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
        self.assertEqual(markdown_to_html(text), expected)

        # Link injection
        text = "[click me](javascript:alert(1))"
        # Assuming no URL validation, it just formats. Telegram clients handle `javascript:` by not clicking or warning.
        expected = '<a href="javascript:alert(1)">click me</a>'
        self.assertEqual(markdown_to_html(text), expected)

    def test_unclosed_tags(self):
        """Test unclosed markdown tags."""
        text = "**bold"
        expected = "**bold" # Should remain as is
        self.assertEqual(markdown_to_html(text), expected)

    def test_backslashes(self):
        """Test backslash escaping."""
        text = r"foo\.bar"
        expected = "foo.bar"
        self.assertEqual(markdown_to_html(text), expected)

        text = r"\*asterisk\*"
        expected = "&#42;asterisk&#42;"
        self.assertEqual(markdown_to_html(text), expected)

    def test_escaped_brackets_and_parenthesis(self):
        """Test escaped brackets and parenthesis do not trigger links."""
        # Escaped brackets should be rendered as entities and not form a link
        text = r"\[text\](url)"
        # Expected: &#91;text&#93;(url)
        expected = "&#91;text&#93;(url)"
        self.assertEqual(markdown_to_html(text), expected)

        # Escaped parenthesis in link text
        text = r"[text \(escaped\)](url)"
        # Link regex matches [text \(escaped\)] ?
        # Step 1: `\(` -> `\(` (preserved).
        # Step 2: `\(` -> `\(`.
        # Step 3: `\(` -> `&#40;`.
        # So text becomes `[text &#40;escaped&#41;](url)`.
        # Link regex `\[([^\]]+)\]`. Matches `text &#40;escaped&#41;`.
        # Result: `<a href="url">text &#40;escaped&#41;</a>`.
        expected = '<a href="url">text &#40;escaped&#41;</a>'
        self.assertEqual(markdown_to_html(text), expected)

if __name__ == '__main__':
    unittest.main()
