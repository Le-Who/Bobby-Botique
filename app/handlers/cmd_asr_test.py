# app/handlers/cmd_asr_test.py
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter
from app.utils.multimodal_processor import transcribe_voice


@authorized_only
@safe_handler("Ошибка при тестировании ASR")
async def asr_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden developer command to test ASR models.
    Usage: Reply to a voice message with `/asr gemini-2.5-pro`
    """
    message = update.message
    if not message.reply_to_message or not message.reply_to_message.voice:
        await message.reply_text("❌ Нужно сделать Reply (Ответить) на голосовое сообщение.")
        return

    if not context.args:
        await message.reply_text("❌ Укажите модель: `/asr gemini-2.5-pro`", parse_mode="Markdown")
        return

    model_name = context.args[0].strip()
    voice = message.reply_to_message.voice

    status_msg = await message.reply_text(f"⏳ Транскрибирую через `{model_name}`...", parse_mode="Markdown")

    try:
        # Download voice bytes
        voice_file = await voice.get_file()
        file_bytes = await voice_file.download_as_bytearray()

        # Transcribe using specific model
        transcript, intent = await transcribe_voice(
            bytes(file_bytes),
            mime_type=voice.mime_type or "audio/ogg",
            model=model_name,
        )

        if transcript:
            formatted_text, parse_mode = TelegramFormatter.format_text(
                f"🎙 **ASR Test** (`{model_name}`):\n\n{transcript}\n\n*Intent:* `{intent}`"
            )
            await status_msg.edit_text(formatted_text, parse_mode=parse_mode)
        else:
            await status_msg.edit_text(f"❌ Модель `{model_name}` вернула пустую расшифровку.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"ASR test failed: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка вызова модели:\n`{str(e)[:200]}`", parse_mode="Markdown")
