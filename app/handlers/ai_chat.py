"""
AI Chat handler — regular conversational chat with context management.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import prompts
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    _resolve_ai_request,
    handle_ai_response_error,
)
from app.repos.chats import update_user_chat
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_CHAT, update_stage


async def _handle_regular_chat(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
    model_override: str | None = None,
):
    # Используем переопределение models, if указано, иначе model from chat_state
    model_for_this_request = model_override or chat_state.model
    _, model_used, resolution = await _resolve_ai_request(model_for_this_request)

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

    # Assemble context with token-budget awareness
    from app.context_assembler import get_assembler

    assembler = get_assembler()

    # Use persisted summary from chat state (survives restarts)
    existing_summary = chat_state.context_summary

    # Compose system instruction first (needed for budget calculation)
    system_instruction = prompts.compose_system_instruction(chat_state.system_prompt)

    # Assemble context within token budget
    assembled = assembler.assemble(
        history=chat_state.history,
        user_message=user_message,
        system_instruction=system_instruction,
        existing_summary=existing_summary,
    )

    # Update chat state with assembled context
    chat_state.history = assembled.history
    chat_state.context_summary = assembled.summary

    if assembled.was_truncated:
        logging.info(
            "Context trimmed for user %s: dropped %d msgs, audit=%s, llm_scheduled=%s",
            user_id, assembled.messages_dropped, assembled.audit_hash,
            assembled.llm_summarization_scheduled,
        )

        # Record summarization metrics
        from app.metrics import role_conv_metrics
        from app.prompt_registry import estimate_tokens_cyrillic

        tokens_saved = sum(
            estimate_tokens_cyrillic(assembler._extract_text(msg))
            for msg in assembled.dropped_messages
        )
        summary_len = estimate_tokens_cyrillic(assembled.summary) if assembled.summary else 0
        tier = "llm" if assembled.llm_summarization_scheduled else "local"
        await role_conv_metrics.record_summarization(
            reason=f"{tier}: dropped {assembled.messages_dropped} msgs",
            tokens_saved=tokens_saved,
            summary_length=summary_len,
        )

        # Schedule async LLM summarization for NEXT request
        if assembled.llm_summarization_scheduled and assembled.dropped_messages:
            async def _store_llm_summary(summary: str) -> None:
                chat_state.context_summary = summary
                await update_user_chat(user_id, chat_state)
                logging.info("LLM summary persisted for user %s", user_id)

            assembler.schedule_llm_summarization(
                dropped_messages=assembled.dropped_messages,
                existing_summary=existing_summary,
                callback=_store_llm_summary,
            )

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
            await update_user_chat(user_id, chat_state)

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
            await update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()
        await update_user_chat(user_id, chat_state)
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
