"""
AI Document handler — processes questions about uploaded documents.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database import ChatState
from app.i18n import t
from app.metrics import metrics_collector
from app.repos.chats import ensure_chat_generation
from app.utils.heartbeat import stop_heartbeat
from app.utils.stage_indicators import STAGES_DOCUMENT, update_stage


def _document_reply_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 Загрузить другой документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
            [InlineKeyboardButton("❌ Отменить работу с документами", callback_data="doc:cancel")],
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
    )


async def _handle_document_question(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
):
    """Keep private document content inside one exact-generation lease."""
    stop_heartbeat(placeholder_message.message_id)
    known_epoch = (
        None
        if getattr(chat_state, "_has_persisted_chat", True) is False
        else int(getattr(chat_state, "memory_epoch", 0))
    )
    expected_epoch = await ensure_chat_generation(user_id, expected_epoch=known_epoch)
    if expected_epoch is None:
        return
    chat_state.memory_epoch = expected_epoch
    chat_state._has_persisted_chat = True

    from app.repos.memory_consent import private_data_lease

    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="conversation:document",
        require_ltm=False,
    ) as lease_current:
        if not lease_current:
            try:
                await placeholder_message.edit_text(t("doc.error_question"))
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
            return
        await _handle_document_question_leased(
            placeholder_message,
            user_id,
            user_message,
            chat_state,
        )


async def _handle_document_question_leased(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
):
    """Обрабатывает вопросы по загруженным документам."""
    try:
        # Get afterдний document user
        from app.document_processor import get_document_content, get_user_documents

        documents = await get_user_documents(user_id)
        if not documents:
            try:
                await placeholder_message.edit_text("❌ У вас нет загруженных документов. Сначала загрузите документ.")
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
                # Fallback на new message
                await placeholder_message.reply_text("❌ У вас нет загруженных документов. Сначала загрузите документ.")
            return

        # Берем самый afterдний document
        latest_document = documents[0]
        document_content = await get_document_content(latest_document["id"], user_id)

        if not document_content:
            try:
                await placeholder_message.edit_text("❌ Не удалось получить содержимое документа.")
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
                # Fallback на new message
                await placeholder_message.reply_text("❌ Не удалось получить содержимое документа.")
            return

        try:
            await update_stage(placeholder_message, STAGES_DOCUMENT, 0)
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # If не можем отредактировать, отправляем new message
            placeholder_message = await placeholder_message.reply_text("📄 Анализирую документ...")

        # Chunk document intelligently based on user's question
        from app.documents.chunking import chunk_for_context

        original_length = len(document_content) if document_content else 0
        document_content = chunk_for_context(
            document_content,
            query=user_message,
            max_context_tokens=8500,  # ~30K chars, matching previous budget
        )
        if len(document_content) < original_length:
            document_content += "\n\n[Документ сокращён до наиболее релевантных фрагментов]"
            logging.info(
                "Document chunked from %d to %d chars (query-aware)",
                original_length,
                len(document_content),
            )

        # Безопасная обработка document_content
        try:
            safe_document_content = str(document_content)
        except Exception as e:
            logging.error("Failed to convert document content to string: %s", e, exc_info=True)
            try:
                await placeholder_message.edit_text("❌ Ошибка обработки содержимого документа.")
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
            return

        content_length = len(safe_document_content) if safe_document_content else 0
        logging.info(
            f"Processing document question for user {user_id}, document: {latest_document['filename']}, content length: {content_length}"
        )

        from app.prompt_registry import FORMATTING_RULES_COMPACT

        # Create промпт for вопроса по documentу
        document_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу документов для Telegram-бота. Твоя задача — ответить на вопрос пользователя по содержимому документа, используя правильное форматирование.

# КОНТЕКСТ
**Содержимое документа:**
{safe_document_content}

**Вопрос пользователя:** {user_message}

# ИНСТРУКЦИИ
1. Внимательно прочитай содержимое документа
2. Найди информацию, относящуюся к вопросу
3. Структурируй ответ логично
4. Примени стандартное Markdown форматирование

{FORMATTING_RULES_COMPACT}

# ВАЖНЫЕ ПРАВИЛА
- Отвечай ТОЛЬКО на основе содержимого документа
- Если информации недостаточно, честно скажи об этом
- Структурируй ответ: краткий ответ → детали → список ключевых элементов

Ответь на вопрос пользователя, основываясь на содержимом документа."""

        parts = [document_prompt] if document_prompt else []
        history = [{"role": "user", "parts": parts}]

        from app.providers.request_factory import generation_request_from_history
        from app.providers.stream_types import Workload
        from app.response_delivery.delivery import (
            TelegramTarget,
            get_telegram_response_delivery,
        )
        from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery
        from app.response_delivery.presentation import FixedPresentation

        request = await generation_request_from_history(
            models=(settings.DEFAULT_MODEL,),
            history=history,
            user_id=user_id,
            chat_id=placeholder_message.chat_id,
            thinking_level=chat_state.thinking_level,
            workload=Workload.INTERACTIVE,
            allow_deferred=False,
        )
        outcome = await get_telegram_response_delivery().stream(
            TelegramTarget(
                placeholder_message=placeholder_message,
                bot=placeholder_message.get_bot(),
                chat_id=placeholder_message.chat_id,
                private_content=True,
            ),
            request,
            presentation=FixedPresentation(
                actions=_document_reply_markup(),
                long_read_title=latest_document["filename"] or "Ответ по документу",
            ),
        )
        if isinstance(outcome, (CompleteDelivery, PartialDelivery)):
            model_used = settings.DEFAULT_MODEL
            metadata = outcome.completion if isinstance(outcome, CompleteDelivery) else outcome.terminal
            route = getattr(metadata, "route", None)
            if route is not None:
                model_used = route.actual_model
            await metrics_collector.record_api_call("document_question", model_used)
    except Exception as e:
        logging.error("Error processing document question: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text(t("doc.error_question"))
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # Fallback на new message
            await placeholder_message.reply_text(t("doc.error_question"))
