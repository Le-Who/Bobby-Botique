"""
AI Agent — thin re-export facade.

This module delegates to domain-specific sub-modules while maintaining
backward compatibility for all existing ``from app.handlers.agent import X``
call-sites.

Sub-modules:
    ai_core      — shared infrastructure (error handling, key resolution)
    ai_search    — QnA, research agent, complex search
    ai_chat      — regular conversational chat
    ai_document  — document question answering
    ai_photo     — photo analysis, media groups
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

# ── Concurrency tiers ────────────────────────────────────────────────────────
from app.adapters.concurrency import heavy_request_semaphore, ultra_heavy_semaphore

# ── Re-exports from ai_chat ──────────────────────────────────────────────────
from app.handlers.ai_chat import _handle_regular_chat  # noqa: F401

# ── Re-exports from ai_core ──────────────────────────────────────────────────
from app.handlers.ai_core import (  # noqa: F401
    _get_ai_response,
    _get_ai_response_with_routing,
    _increment_key_usage,
    _resolve_ai_request,
    handle_ai_response_error,
)

# ── Re-exports from ai_document ──────────────────────────────────────────────
from app.handlers.ai_document import _handle_document_question  # noqa: F401

# ── Re-exports from ai_photo ─────────────────────────────────────────────────
from app.handlers.ai_photo import (  # noqa: F401
    _download_images_concurrently,
    _handle_complex_media_group_search,
    _handle_media_group_photos,
    _handle_photo,
    process_media_group_request,
)

# ── Re-exports from ai_search ────────────────────────────────────────────────
from app.handlers.ai_search import (  # noqa: F401
    _handle_complex_agent_search,
    _handle_qna_search,
    _handle_research_agent,
)
from app.repos.chats import get_user_chat
from app.utils.heartbeat import stop_heartbeat

# ── Orchestration (stays here — thin dispatcher) ─────────────────────────────


async def process_long_request(
    placeholder_message: Message, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        effective_msg = update.effective_message
        is_photo = bool(effective_msg.photo) if effective_msg else False
        # Support edit-in-place: handle_edited_request injects the corrected text here
        _text_override = (
            context.user_data.get("_edited_text_override")
            if context.user_data
            else None
        )
        if _text_override is not None:
            text = _text_override
        elif effective_msg:
            text = effective_msg.text or effective_msg.caption or ""
        else:
            text = ""
        chat_state = await get_user_chat(update.effective_user.id)

        # ── Classify request tier ────────────────────────────────────────
        # Ultra-heavy: all agentic/search paths (high RAM, multiple LLM calls)
        # Regular: conversational chat and simple photo analysis
        is_ultra_heavy = (
            text.startswith(("?", "??"))
            or (is_photo and text.startswith(("?", "??")))
            or (not is_photo and chat_state.search_enabled)
            or chat_state.is_deep_dive
        )
        semaphore = ultra_heavy_semaphore if is_ultra_heavy else heavy_request_semaphore

        async with semaphore:
            if is_photo and (text.startswith(("?", "??"))):
                keyboard = [
                    [InlineKeyboardButton("🖼️ Только описать фото", callback_data="complex:vision_only")],
                    [InlineKeyboardButton("🔎 Выполнить сложный поиск", callback_data="complex:confirm")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="complex:cancel")],
                ]

                # Save оригинальное message в contextе
                if not hasattr(context, "user_data"):
                    context.user_data = {}
                if context.user_data is not None:
                    context.user_data["original_message"] = update.message

                # Не удаляем placeholder message, а редактируем его
                try:
                    stop_heartbeat(placeholder_message.message_id)
                    await placeholder_message.edit_text(
                        "Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception as edit_error:
                    logging.error("Could not edit placeholder message: %s", edit_error)
                    # If не можем отредактировать, отправляем new message
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                return

            if is_photo and update.effective_message:
                await _handle_photo(placeholder_message, update.effective_message, chat_state)
                return

            if text.startswith("??"):
                await _handle_research_agent(
                    placeholder_message,
                    update.effective_user.id,
                    text[2:].strip(),
                    chat_state,
                )
            elif text.startswith("?"):
                await _handle_qna_search(placeholder_message, text[1:].strip(), chat_state)
            elif chat_state.search_enabled:
                await _handle_research_agent(placeholder_message, update.effective_user.id, text, chat_state)
            elif chat_state.is_deep_dive:
                # Continue deep dive session — route follow-ups through agentic search
                await _handle_research_agent(placeholder_message, update.effective_user.id, text, chat_state)
            else:
                await _handle_regular_chat(placeholder_message, update.effective_user.id, text, chat_state)

    except Exception as e:
        logging.error("Error in background task dispatcher: %s", e, exc_info=True)
        try:
            from app.errors import build_retry_and_roles_keyboard, user_friendly_error

            friendly = user_friendly_error(e)
            stop_heartbeat(placeholder_message.message_id)
            await placeholder_message.edit_text(friendly, reply_markup=build_retry_and_roles_keyboard())
        except Exception as inner_e:
            logging.error("Could not edit placeholder message: %s", inner_e)
