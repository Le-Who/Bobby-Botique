"""Direct text-to-speech ingress; the voice queue owns synthesis and delivery."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import replace
from typing import TYPE_CHECKING

from telegram import Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.i18n import detect_language, t
from app.metrics import metrics_collector
from app.middleware.debounce import DebounceResult, SpeechIngressReservation, has_hard_speech_boundary
from app.voice_intent import SpeechRequestIntent, SpeechSourcePart, build_voice_source_key, resolve_speech_request

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.providers.tts_tokens import SpeechTokenReceipt


def _is_short_source_tail(previous: str, tail: str) -> bool:
    previous, tail = previous.strip(), tail.strip()
    return bool(previous and tail and len(re.findall(r"\S+", tail)) <= 80 and len(tail) * 5 <= len(previous))


async def _group_short_source_tail(
    parts: tuple[SpeechSourcePart, ...],
    *,
    contiguous: bool,
    user_id: int,
    epoch: int,
    voice: str,
    temperature: float | None,
) -> tuple[tuple[SpeechSourcePart, ...], dict[int, tuple[SpeechTokenReceipt, ...]]]:
    """Merge only an adjacent small final continuation with verified admission."""
    if not contiguous or len(parts) < 2 or not _is_short_source_tail(parts[-2].text, parts[-1].text):
        return parts, {}
    from app.repos.memory_consent import private_data_lease
    from app.voice_engine import plan_voice_source_merge

    previous, tail = parts[-2:]
    text = previous.text + tail.text
    try:
        async with private_data_lease(user_id, epoch, purpose="conversation:tts_plan", require_ltm=False) as current:
            if not current:
                return parts, {}
            admission = await plan_voice_source_merge(response_text=text, voice=voice, tts_temperature=temperature)
        if not admission.allowed:
            return parts, {}
    except Exception as error:
        logger.warning("Speech tail admission unavailable: %s", type(error).__name__)
        return parts, {}
    merged = replace(
        previous,
        text=text,
        merged_source_ids=(*previous.merged_source_ids, tail.reply_to_message_id, *tail.merged_source_ids),
    )
    receipts = {previous.reply_to_message_id: admission.token_receipts} if admission.token_receipts else {}
    return (*parts[:-2], merged), receipts


def speech_request_for_message(message: Message, batch: DebounceResult | None = None) -> SpeechRequestIntent:
    """Retain reply targets of absorbed commands and never parse forward bodies."""
    if batch and getattr(batch, "speech_collection", False):
        error = batch.speech_error
        if error:
            return SpeechRequestIntent(
                "clarify",
                expected_epoch=batch.speech_epoch,
                error=error,
                source_message_ids=tuple(item.message.message_id for item in batch.entries),
            )
        first, *continuations = sorted(batch.entries, key=lambda item: item.message.message_id)
        request = resolve_speech_request(user_texts=[first.text])
        if request.action != "speak":
            return SpeechRequestIntent("clarify")
        trailing_whitespace = first.text[len(first.text.rstrip()) :]
        parts = [SpeechSourcePart(request.text + trailing_whitespace, first.message.message_id)]
        parts.extend(SpeechSourcePart(item.text, item.message.message_id) for item in continuations)
        parts = _repair_physical_seams(
            parts, {item.message.message_id for item in batch.entries if has_hard_speech_boundary(item.text)}
        )
        if any(not part.text.strip() for part in parts):
            return SpeechRequestIntent(
                "clarify",
                expected_epoch=batch.speech_epoch,
                error="unbreakable_seam",
                source_message_ids=tuple(item.message.message_id for item in batch.entries),
            )
        return replace(
            request,
            text="".join(part.text for part in parts),
            parts=tuple(parts),
            expected_epoch=batch.speech_epoch,
            contiguous_parts=True,
        )
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
    request = resolve_speech_request(
        user_texts=user_texts,
        reply_texts=reply_texts,
        forwarded_texts=[entry.text for entry in batch.forwarded_entries] if batch else [],
    )
    if request.action != "speak":
        return request
    parts = []
    if request.source == "forwarded" and batch:
        parts = [
            SpeechSourcePart(item.text, getattr(item.message, "message_id", message.message_id))
            for item in batch.forwarded_entries
            if item.text.strip()
        ]
    elif request.source == "reply":
        for item in authored:
            reply = getattr(item, "reply_to_message", None)
            if reply and (reply.text or reply.caption or "").strip():
                parts = [SpeechSourcePart(request.text, getattr(reply, "message_id", message.message_id))]
                break
    elif request.source == "inline" and batch and batch.user_entries:
        for source_entry in batch.user_entries:
            individual = resolve_speech_request(user_texts=[source_entry.text])
            text = (
                individual.text
                if individual.action == "speak"
                else source_entry.text
                if individual.action == "none"
                else ""
            )
            if text.strip():
                parts.append(SpeechSourcePart(text, getattr(source_entry.message, "message_id", message.message_id)))
    parts.sort(key=lambda part: part.reply_to_message_id)
    return replace(request, parts=tuple(parts))


def _repair_physical_seams(parts: list[SpeechSourcePart], hard_seams: set[int]) -> list[SpeechSourcePart]:
    """Move a split lexical token across a physical seam, preserving every scalar."""
    parts = list(parts)
    for index in range(len(parts) - 1):
        left, right = parts[index], parts[index + 1]
        if left.reply_to_message_id not in hard_seams:
            # Desktop trims the source separator on both sides of a natural
            # cut. Restore one spoken separator, without inventing a paragraph.
            if left.text and right.text and not left.text[-1].isspace() and not right.text[0].isspace():
                parts[index] = replace(left, text=left.text + " ")
            continue
        if not left.text or not right.text or not (_lexical_scalar(left.text[-1]) and _lexical_scalar(right.text[0])):
            continue
        boundary = len(left.text)
        while boundary and _lexical_scalar(left.text[boundary - 1]):
            boundary -= 1
        prefix, suffix = left.text[:boundary], left.text[boundary:]
        parts[index] = replace(left, text=prefix)
        parts[index + 1] = replace(right, text=suffix + right.text)
    return parts


def _lexical_scalar(value: str) -> bool:
    return value.isalnum() or unicodedata.category(value).startswith("M") or value in "_'’-‑"


async def try_handle_speech_collection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reservation: SpeechIngressReservation | None = None
) -> bool:
    """Intercept explicit split sources before state inputs, commands and chat."""
    import asyncio

    from app import state
    from app.handlers.messages import _release_admission, _send_busy_ephemeral
    from app.middleware.debounce import (
        accepts_speech_message,
        capture_reserved_speech_message,
        debounce_message,
        speech_collection_epoch,
        starts_long_speech,
        wait_for_speech_collection,
    )
    from app.repos.chats import get_user_chat

    message, user = update.effective_message, update.effective_user
    if message is None or user is None or not message.text:
        return False
    if reservation is not None and reservation.continuation:
        if not await capture_reserved_speech_message(user.id, message, reservation, bot=context.bot):
            await message.reply_text(
                t("speech.capacity" if reservation.capacity else "speech.unavailable", detect_language(message.text))
            )
        return True
    if accepts_speech_message(user.id, message):
        if not await wait_for_speech_collection(user.id, message):
            await message.reply_text(t("speech.unavailable", detect_language(message.text)))
            return True
        await debounce_message(user.id, message, bot=context.bot, speech_epoch=speech_collection_epoch(user.id))
        return True
    if not starts_long_speech(message):
        return False
    user_state = state.get_user_state(user.id)
    if user_state.is_processing or state.get_user_lock(user.id).locked():
        await _send_busy_ephemeral(update)
        return True
    owner = asyncio.current_task()
    assert owner is not None
    user_state.is_processing = True
    state.register_active_task(user.id, owner)
    try:

        async def load_speech_epoch() -> int | None:
            chat_state = await get_user_chat(user.id)
            return chat_state.memory_epoch

        batch = await debounce_message(user.id, message, bot=context.bot, load_speech_epoch=load_speech_epoch)
        if batch is not None:
            await handle_speech_request(update, context, speech_request_for_message(message, batch))
    finally:
        _release_admission(user.id, owner)
    return True


async def handle_speech_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, request: SpeechRequestIntent
) -> None:
    """Enqueue the exact source or ask for it; never fall back to generated text."""
    user = update.effective_user
    assert user is not None
    started = time.monotonic()
    try:
        success = await _enqueue_speech_request(update, context, request)
    except Exception:
        await metrics_collector.record_request(
            "direct_speech", time.monotonic() - started, success=False, user_id=user.id
        )
        raise
    await metrics_collector.record_request(
        "direct_speech", time.monotonic() - started, success=success, user_id=user.id
    )


async def _enqueue_speech_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, request: SpeechRequestIntent
) -> bool:
    """Return admission outcome; cancelled submissions are not completed requests."""
    from app.repos.chats import get_user_chat
    from app.voice_engine import fire_voice_reply

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    assert message is not None and user is not None and chat is not None
    lang = detect_language(message.text)
    if request.action == "clarify":
        if request.error:
            reason = {
                "stale_epoch": "изменились настройки приватности. ",
                "unbreakable_seam": "одно слово занимает целую исходную часть и пересекает её границу. ",
                "collection_capacity": "превышен предел сбора (32 части или 256 КиБ). ",
                "source_admission": "одна из исходных частей не прошла допуск или её обработка отменена. ",
            }.get(request.error, "за 60 секунд не получен полный конец текста. ")
            notice = (
                "Сбор текста завершён без озвучивания: "
                + reason
                + "Исходные сообщения остаются в чате. Повторите «Озвучь» в ответ на каждую исходную часть "
                "или отправьте текст заново с короткой последней частью; «Готово» подтверждает полный текст."
            )
            await _notify_source(context.bot, chat.id, message.message_id, notice, message=message)
            for source_id in request.source_message_ids:
                if source_id != message.message_id:
                    await _notify_source(context.bot, chat.id, source_id, notice)
            return False
        await message.reply_text(t("speech.clarify", lang))
        return True
    parts = request.parts or (SpeechSourcePart(request.text, message.message_id),)
    shared_cleanup = request.contiguous_parts and len(parts) > 1
    if shared_cleanup:
        from app.providers.tts import clean_speech_parts

        # Formatting can cross a client cut. Clean one contiguous source while
        # keeping surviving text attached to the original Telegram message.
        cleaned = clean_speech_parts([part.text for part in parts])
        parts = tuple(replace(part, text=text) for part, text in zip(parts, cleaned, strict=True))
    try:
        chat_state = await get_user_chat(user.id)
    except Exception:
        logger.exception("Direct speech enqueue failed")
        await message.reply_text(t("speech.unavailable", lang))
        return False
    epoch = request.expected_epoch if request.expected_epoch is not None else chat_state.memory_epoch
    parts, token_receipts = await _group_short_source_tail(
        parts,
        contiguous=request.contiguous_parts,
        user_id=user.id,
        epoch=epoch,
        voice=chat_state.voice_id or "",
        temperature=chat_state.tts_temperature,
    )
    success = True
    for part in parts:
        if shared_cleanup and not part.text.strip():
            success = False
            await _notify_source(context.bot, chat.id, part.reply_to_message_id, t("speech.empty", lang))
            continue
        try:
            result = await fire_voice_reply(
                bot=context.bot,
                user_id=user.id,
                chat_id=chat.id,
                reply_to_message_id=part.reply_to_message_id,
                response_text=part.text,
                voice=chat_state.voice_id or "",
                tts_temperature=chat_state.tts_temperature,
                source_key=build_voice_source_key("direct_speech", chat.id, part.reply_to_message_id, part.text),
                expected_epoch=request.expected_epoch
                if request.expected_epoch is not None
                else chat_state.memory_epoch,
                **({"text_is_clean": True} if shared_cleanup else {}),
                **(
                    {"token_receipts": token_receipts[part.reply_to_message_id]}
                    if part.reply_to_message_id in token_receipts
                    else {}
                ),
            )
            if result.queued or result.deduped:
                if result.queued:
                    for source_id in part.merged_source_ids:
                        await _notify_source(context.bot, chat.id, source_id, t("speech.tail_joined", lang))
                continue
            key = {
                "capacity": "speech.capacity",
                "user_capacity": "speech.capacity",
                "text_too_large": "speech.too_large",
                "empty_text": "speech.clarify",
            }.get(result.reason_code, "speech.unavailable")
        except Exception:
            logger.exception("Direct speech source enqueue failed")
            key = "speech.unavailable"
        success = False
        # Every physical source receives its own rejection, including parts
        # following a partial queue admission. Never silently drop the tail.
        for source_id in (part.reply_to_message_id, *part.merged_source_ids):
            await _notify_source(
                context.bot,
                chat.id,
                source_id,
                t(key, lang),
                message=message if source_id == message.message_id else None,
            )
    return success


async def _notify_source(bot, chat_id: int, source_id: int, text: str, *, message: Message | None = None) -> None:
    """A deleted reply target cannot prevent admission or notice for later parts."""
    try:
        if message is not None:
            await message.reply_text(text)
        else:
            await bot.send_message(chat_id=chat_id, reply_to_message_id=source_id, text=text)
        return
    except BadRequest:
        try:
            await bot.send_message(chat_id=chat_id, text=f"Часть {source_id}: {text}")
            return
        except Exception as error:
            logger.warning("Speech source notice unavailable: %s", type(error).__name__)
    except Exception as error:
        logger.warning("Speech source notice unavailable: %s", type(error).__name__)
