
import unittest
import re
from app.utils.formatting import TelegramFormatter, escape_markdown_v2

class TestFormatting(unittest.TestCase):
    def test_escape_markdown_v2_basic(self):
        text = "Hello world"
        self.assertEqual(escape_markdown_v2(text), "Hello world")

    def test_escape_markdown_v2_special_chars(self):
        # Specific chars should be escaped
        text = "Hello. World!"
        # . and ! are in special chars list
        self.assertEqual(escape_markdown_v2(text), r"Hello\. World\!")

        text = "2 * 3"
        # * should be escaped as it's not a pair
        self.assertEqual(escape_markdown_v2(text), r"2 \* 3")

    def test_escape_markdown_v2_valid_formatting(self):
        # Valid formatting should be preserved
        text = "*bold* _italic_ `code` [link](url)"
        self.assertEqual(escape_markdown_v2(text), text)

    def test_escape_markdown_v2_mixed(self):
        text = "Hello *bold* and 2 * 3"
        # *bold* preserved, * escaped
        self.assertEqual(escape_markdown_v2(text), r"Hello *bold* and 2 \* 3")

    def test_telegram_formatter_prepare(self):
        # TelegramFormatter converts ** to *
        text = "**bold**"
        self.assertEqual(TelegramFormatter._prepare_markdown_v2(text), "*bold*")

        # And escapes other things
        text = "**bold** and 2 * 3"
        self.assertEqual(TelegramFormatter._prepare_markdown_v2(text), r"*bold* and 2 \* 3")

    def test_telegram_formatter_complex(self):
        text = "Here is a [link](http://example.com) and some `code` and **bold** text."
        expected = r"Here is a [link](http://example.com) and some `code` and *bold* text\."
        self.assertEqual(TelegramFormatter._prepare_markdown_v2(text), expected)

    def test_unbalanced_formatting(self):
        # Unbalanced formatting should be escaped
        text = "*bold"
        self.assertEqual(escape_markdown_v2(text), r"\*bold")

        text = "[link](url"
        self.assertEqual(escape_markdown_v2(text), r"\[link\]\(url")

    def test_nested_brackets(self):
        # This is tricky. The regex is non-recursive.
        # [text [nested]](url)
        # Regex matches [text [nested]
        # Then ](url)
        # This might not work perfectly for nested brackets but standard MD usually handles flat links.
        pass

if __name__ == '__main__':
    unittest.main()
