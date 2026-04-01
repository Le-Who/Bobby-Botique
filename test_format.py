import sys

sys.path.insert(0, r"d:\gemaibotv2\gemaibotv2")
from app.utils.formatting import TelegramFormatter

t = """🎙️ **Распознанный текст**
_аниме-девушки с воющимися формами._

🎨 **Подтвердите запрос к ИИ-художнику:**
`аниме\-девушки с воющимися формами\.`"""

form, _ = TelegramFormatter.format_text(t)
print("FORMATTED:")
print(form)
