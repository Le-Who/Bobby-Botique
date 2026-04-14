import sys

sys.path.insert(0, r"d:\gemaibotv2\gemaibotv2")
from app.utils.text_format import markdown_to_html

text = r"""🎙️ **Распознанный текст**
_аниме-девушки с воющимися формами._

🎨 **Подтвердите запрос к ИИ-художнику:**
`аниме\-девушки с воющимися формами\.`"""

html_out = markdown_to_html(text)
print("=== Python Output ===")
print(repr(html_out))

# To be 100% sure, let's also pass it to python-telegram-bot's parse check?
# The error says "unmatched end tag at byte offset 45".
# This happens in Telegram's backend, not locally. But we can see what the string is.

# bytes count of HTML output
for i, char in enumerate(html_out):
    print(i, repr(char))
