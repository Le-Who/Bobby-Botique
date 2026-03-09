"""
AI Document handler — processes questions about uploaded documents.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    handle_ai_response_error,
)
from app.metrics import metrics_collector
from app.utils.heartbeat import stop_heartbeat
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_DOCUMENT, update_stage


async def _handle_document_question(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
):
    """Обрабатывает вопросы по загруженным документам"""
    stop_heartbeat(placeholder_message.message_id)
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

        # Ограничиваем размер contextа documentа
        max_context_length = 30000  # Ограничиваем до 30K символов
        original_length = len(document_content) if document_content else 0
        if document_content and len(document_content) > max_context_length:
            document_content = document_content[:max_context_length] + "\n\n[Документ обрезан для экономии токенов]"
            logging.info("Document content truncated from %d to %d characters", original_length, len(document_content))

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

        # Stream via unified ProviderRouter
        # We need the current model, so we resolve it first to pass to stream_and_display
        from app.handlers.ai_core import _resolve_ai_request
        from app.providers import get_provider_router
        from app.streaming import stream_and_display

        _, model_used, _ = await _resolve_ai_request(settings.DEFAULT_MODEL)

        parts = [document_prompt] if document_prompt else []
        history = [{"role": "user", "parts": parts}]

        response_text, success, stream_last_msg = await stream_and_display(
            placeholder_message,
            model_name=model_used,
            history=history,
            system_instruction=None,
            thinking_level=chat_state.thinking_level,
            user_id=user_id,
            bot=placeholder_message.get_bot(),
            chat_id=placeholder_message.chat_id,
            chat_type=placeholder_message.chat.type,
        )

        streamed = bool(success and response_text)

        if not streamed:
            response_text, _ = await _get_ai_response_with_routing(
                settings.DEFAULT_MODEL,
                history,
                user_id=user_id,
                chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
            )

        if response_text:
            # Check, является ли response ошибкой
            from app.errors import build_retry_and_roles_keyboard

            # Используем универсальную функцию обработки ошибок
            if await handle_ai_response_error(response_text, placeholder_message):
                return  # Error обработана, выходим
            else:
                # Успешный response - показываем обычные buttons for documentов
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "📄 Загрузить другой документ",
                            callback_data="doc:upload_new",
                        )
                    ],
                    [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
                    [
                        InlineKeyboardButton(
                            "❌ Отменить работу с документами",
                            callback_data="doc:cancel",
                        )
                    ],
                    [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
                    [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
                ]

                # Send response с buttonми (update existing if streamed, otherwise send new)
                if not streamed:
                    await send_long_message(
                        placeholder_message,
                        response_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                else:
                    button_msg = stream_last_msg if stream_last_msg else placeholder_message
                    try:
                        await button_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                    except Exception as e:
                        if "not modified" not in str(e).lower():
                            logging.warning("Final button edit failed: %s", e)

                await metrics_collector.record_api_call("document_question", settings.DEFAULT_MODEL)
        else:
            try:
                from app.errors import build_retry_and_roles_keyboard

                await placeholder_message.edit_text(
                    "❌ Не удалось получить ответ от AI.",
                    reply_markup=build_retry_and_roles_keyboard(),
                )
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
                # Fallback на new message
                try:
                    from app.errors import build_retry_and_roles_keyboard

                    await placeholder_message.reply_text(
                        "❌ Не удалось получить ответ от AI.",
                        reply_markup=build_retry_and_roles_keyboard(),
                    )
                except Exception:
                    pass

    except Exception as e:
        logging.error("Error processing document question: %s", e, exc_info=True)
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при обработке вопроса по документу. Попробуйте ещё раз."
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
            # Fallback на new message
            await placeholder_message.reply_text(
                "❌ Произошла ошибка при обработке вопроса по документу. Попробуйте ещё раз."
            )
