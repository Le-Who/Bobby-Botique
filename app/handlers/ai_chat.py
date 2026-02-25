"""
AI Chat handler — regular conversational chat with context management.
"""

import logging
from typing import Optional

from telegram import Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app import database as db
from app import prompts
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import update_stage, STAGES_CHAT

from app.handlers.ai_core import (
    handle_ai_response_error,
    _resolve_ai_request,
    _get_ai_response_with_routing,
)


async def _handle_regular_chat(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: db.ChatState,
    model_override: Optional[str] = None,
):
    # Используем переопределение models, if указано, иначе model from chat_state
    model_for_this_request = model_override or chat_state.model
    ai_key, model_used, resolution = await _resolve_ai_request(model_for_this_request)

    if resolution == "all_exhausted":
        # Определяем провайдер на основе models
        is_openrouter = (
            "/" in model_for_this_request if model_for_this_request else False
        )
        provider_name = "OpenRouter" if is_openrouter else "Gemini"
        try:
            await placeholder_message.edit_text(
                f"🚫 Все лимиты для всех моделей {provider_name} на сегодня исчерпаны. Попробуйте позже."
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    if resolution == "confirm_fallback":
        keyboard = [
            [
                InlineKeyboardButton(
                    f"Да, использовать {model_used}",
                    callback_data=f"fallback:confirm:{model_used}",
                )
            ],
            [InlineKeyboardButton("Нет, отмена", callback_data="fallback:cancel")],
        ]
        try:
            await placeholder_message.edit_text(
                f"Все лимиты для модели `{model_for_this_request}` на сегодня исчерпаны.\n"
                f"Однако, я могу выполнить ваш запрос, используя `{model_used}`. Качество ответа может быть другим.\n"
                "Продолжить?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    # Подготавливаем context с учётом limitов tokenов

    # Extract суммарfromацию from истории, if есть
    summary = None
    if (
        chat_state.history
        and isinstance(chat_state.history, list)
        and len(chat_state.history) > 0
    ):
        # Check, есть ли суммарfromация в первом сообщении
        first_msg = chat_state.history[0]
        if (
            isinstance(first_msg, dict)
            and "role" in first_msg
            and "parts" in first_msg
            and len(first_msg["parts"]) > 0
            and isinstance(first_msg["parts"][0], str)
            and "[Суммаризация предыдущего контекста]" in first_msg["parts"][0]
        ):
            summary = first_msg["parts"][0]
            # Убираем суммарfromацию from истории for обработки
            chat_state.history = chat_state.history[1:]

    # Подготавливаем context с limitами
    prepared_history, new_summary = prompts.prepare_context_with_limits(
        chat_state.history, user_message, summary
    )

    # Строим финальный context
    final_context = prompts.build_context_with_summary(
        prepared_history, new_summary, user_message
    )

    # Update history в chat_state
    chat_state.history = final_context

    # Используем системную инструкцию user or инструкцию by default
    system_instruction = prompts.compose_system_instruction(chat_state.system_prompt)

    try:
        await update_stage(placeholder_message, STAGES_CHAT, 0)
    except Exception as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text(
            f"🧠 Модель {model_used} думает..."
        )

    # Используем обертку с ротацией keyей и health-scoring
    response_text, new_token_count = await _get_ai_response_with_routing(
        model_used,
        chat_state.history,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
    )

    if response_text:
        # Check, является ли response ошибкой
        from app.errors import build_retry_and_roles_keyboard

        # Используем универсальную функцию обработки ошибок
        async def cleanup_on_error() -> None:
            chat_state.history.pop()  # Убираем добавленный промпт
            await db.update_user_chat(user_id, chat_state)

        if await handle_ai_response_error(
            response_text, placeholder_message, on_error_callback=cleanup_on_error
        ):
            return  # Error обработана, выходим
        else:
            # Успешный response - добавляем в history и показываем обычные buttons
            buttons = [
                [
                    InlineKeyboardButton(
                        "🔄 Попробовать ещё раз", callback_data="retry_last"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎭 Выбрать роль ИИ", callback_data="open_roles"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✨ Начать новую тему",
                        callback_data="deepdive:new_topic"
                        if chat_state.is_deep_dive
                        else "new_topic",
                    )
                ],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)

            try:
                await send_long_message(
                    placeholder_message, response_text, reply_markup=reply_markup
                )
            except Exception as send_err:
                logging.warning(
                    f"send_long_message failed, fallback to reply_text: {send_err}"
                )
                try:
                    from app.utils.formatting import TelegramFormatter

                    formatted_text, parse_mode = TelegramFormatter.format_text(
                        response_text
                    )
                    await placeholder_message.reply_text(
                        formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
                    )
                except Exception:
                    await placeholder_message.reply_text(
                        response_text, reply_markup=reply_markup
                    )
            chat_state.history.append({"role": "model", "parts": [response_text]})
            chat_state.token_count = new_token_count
            await db.update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()
        await db.update_user_chat(user_id, chat_state)
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
