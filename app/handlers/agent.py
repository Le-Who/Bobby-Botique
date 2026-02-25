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
from typing import List

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app import database as db

# ── Re-exports from ai_core ──────────────────────────────────────────────────
from app.handlers.ai_core import (  # noqa: F401
    handle_ai_response_error,
    _resolve_ai_request,
    _get_ai_response,
    _get_ai_response_with_routing,
    _increment_key_usage,
)

# ── Re-exports from ai_search ────────────────────────────────────────────────
from app.handlers.ai_search import (  # noqa: F401
    _handle_qna_search,
    _handle_research_agent,
    _handle_complex_agent_search,
)

# ── Re-exports from ai_chat ──────────────────────────────────────────────────
from app.handlers.ai_chat import _handle_regular_chat  # noqa: F401

# ── Re-exports from ai_document ──────────────────────────────────────────────
from app.handlers.ai_document import _handle_document_question  # noqa: F401

# ── Re-exports from ai_photo ─────────────────────────────────────────────────
from app.handlers.ai_photo import (  # noqa: F401
    _handle_photo,
    process_media_group_request,
    _download_images_concurrently,
    _handle_media_group_photos,
    _handle_complex_media_group_search,
)


# ── Orchestration (stays here — thin dispatcher) ─────────────────────────────

async def process_long_request(
    placeholder_message: Message, update: Update, context: ContextTypes.DEFAULT_TYPE
):
    try:
        is_photo = bool(update.message.photo)
        text = update.message.text or update.message.caption or ""
        chat_state = await db.get_user_chat(update.effective_user.id)

        if is_photo and (text.startswith("?") or text.startswith("??")):
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🖼️ Только описать фото", callback_data="complex:vision_only"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔎 Выполнить сложный поиск", callback_data="complex:confirm"
                    )
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data="complex:cancel")],
            ]

            # Save оригинальное message в contextе
            if not hasattr(context, "user_data"):
                context.user_data = {}
            context.user_data["original_message"] = update.message

            # Не удаляем placeholder message, а редактируем его
            try:
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

        if is_photo:
            await _handle_photo(placeholder_message, update.message, chat_state)
            return

        # Check, есть ли у user documents for вопросов
        from app.document_processor import get_user_documents

        # user_documents используется for проверки наличия documentов
        await get_user_documents(update.effective_user.id)

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
            await _handle_research_agent(
                placeholder_message, update.effective_user.id, text, chat_state
            )
        else:
            await _handle_regular_chat(
                placeholder_message, update.effective_user.id, text, chat_state
            )

    except Exception as e:
        logging.error("Error in background task dispatcher: %s", e, exc_info=True)
        try:
            from app.errors import user_friendly_error, build_retry_and_roles_keyboard

            friendly = user_friendly_error(e)
            await placeholder_message.edit_text(
                friendly, reply_markup=build_retry_and_roles_keyboard()
            )
        except Exception as inner_e:
            logging.error("Could not edit placeholder message: %s", inner_e)
