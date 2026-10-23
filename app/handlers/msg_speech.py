"""Direct text-to-speech ingress; the voice queue owns synthesis and delivery."""

import logging

from telegram import Message, Update
from telegram.ext import ContextTypes

from app.i18n import detect_language, t
from app.middleware.debounce import DebounceResult
from app.voice_intent import SpeechRequestIntent, build_voice_source_key, resolve_speech_request

logger = logging.getLogger(__name__)


def speech_request_for_message(message: Message, batch: DebounceResult | None = None) -> SpeechRequestIntent:
    """Retain reply targets of absorbed commands and never parse forward bodies."""
    if batch and batch.user_entries:
        user_texts = [entry.text for entry in batch.user_entries]
        authored = [entry.message for entry in batch.user_entries]
    elif getattr(message, "forward_origin", None):
        user_texts, authored = [], []
    else:
        user_texts, authored = [message.text or ""], [message]
    reply_texts = []
    for item in authored:
        reply = getattr(item, "reply_to_message", None)
        if reply is not None:
            reply_texts.append(reply.text or reply.caption or "")
    return resolve_speech_request(
        user_texts=user_texts,
        reply_texts=reply_texts,
        forwarded_texts=[entry.text for entry in batch.forwarded_entries] if batch else [],
    )


async def handle_speech_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, request: SpeechRequestIntent
) -> None:
    """Enqueue the exact source or ask for it; never fall back to generated text."""
    from app.repos.chats import get_user_chat
    from app.voice_engine import fire_voice_reply

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    assert message is not None and user is not None and chat is not None
    lang = detect_language(message.text)
    if request.action == "clarify":
        await message.reply_text(t("speech.clarify", lang))
        return
    try:
        chat_state = await get_user_chat(user.id)
        result = await fire_voice_reply(
            bot=context.bot,
            user_id=user.id,
            chat_id=chat.id,
            reply_to_message_id=message.message_id,
            response_text=request.text,
            voice=chat_state.voice_id or "",
            tts_temperature=chat_state.tts_temperature,
            source_key=build_voice_source_key("direct_speech", chat.id, message.message_id, request.text),
            expected_epoch=chat_state.memory_epoch,
        )
        if result.queued or result.deduped:
            return
        key = {
            "capacity": "speech.capacity",
            "user_capacity": "speech.capacity",
            "text_too_large": "speech.too_large",
            "empty_text": "speech.clarify",
        }.get(result.reason_code, "speech.unavailable")
    except Exception:
        logger.exception("Direct speech enqueue failed")
        key = "speech.unavailable"
    await message.reply_text(t(key, lang))
