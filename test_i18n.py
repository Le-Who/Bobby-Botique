import sys

sys.path.insert(0, r"d:\gemaibotv2\gemaibotv2")
from app.i18n import t
from app.utils.text_format import markdown_to_html

lang = "ru"
transcript = "аниме-девушки с воющимися формами."
draw_prompt = transcript
label = t("voice.transcript_label", lang)

auto_text = f"🎙️ **{label}**\n_{transcript}_\n\n🎨 **Подтвердите запрос к ИИ-художнику:**\n`{draw_prompt}`"
print("ORIGINAL_MD:")
print(repr(auto_text))

html_out = markdown_to_html(auto_text)
print("\nHTML_OUT:")
print(html_out)
