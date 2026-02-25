"""
AI Search handlers — QnA quick answers, research agent, and complex agent search.
"""

import logging
import json
import asyncio
from typing import Optional

from telegram import Message, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings, get_openrouter_keys
from app import database as db
from app import services
from app.utils.messaging import send_long_message
from app import prompts
from app.metrics import metrics_collector
from app.utils.formatting import escape_format_chars
from app.utils.stage_indicators import update_stage, STAGES_SEARCH_QUICK, STAGES_SEARCH_DEEP

from app.handlers.ai_core import (
    handle_ai_response_error,
    _resolve_ai_request,
    _get_ai_response_with_routing,
)


async def _handle_qna_search(
    placeholder_message: Message,
    user_message: str,
    chat_state: db.ChatState,
    search_query: str = None,
):
    # Если передан search_query, используем его для поиска, а user_message для локализации
    actual_search_query = search_query if search_query else user_message
    # chat_state используется для совместимости с другими функциями

    await metrics_collector.record_search_query()

    try:
        await update_stage(placeholder_message, STAGES_SEARCH_QUICK, 0)
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        placeholder_message = await placeholder_message.reply_text(
            "🔎 Ищу быстрый ответ..."
        )

    # Получаем user_id и chat_id для логирования
    user_id = (
        placeholder_message.from_user.id if placeholder_message.from_user else None
    )
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None

    search_result = await services.tavily_search_agent(
        actual_search_query, search_type="qna", user_id=user_id, chat_id=chat_id
    )
    if search_result.get("error"):
        try:
            await placeholder_message.edit_text(search_result["error"])
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    tavily_answer = search_result.get("answer", "Не удалось найти прямой ответ.")
    try:
        await update_stage(placeholder_message, STAGES_SEARCH_QUICK, 1)
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        placeholder_message = await placeholder_message.reply_text(
            "🌍 Адаптирую ответ..."
        )

    # Используем модель из chat_state, если она указана, иначе используем настройки по умолчанию
    preferred_model = (
        chat_state.model
        if chat_state.model
        else (
            settings.OPENROUTER_QNA_MODEL
            if get_openrouter_keys()
            else settings.QNA_MODEL
        )
    )

    # Экранируем фигурные скобки в данных для предотвращения ошибок форматирования
    safe_user_message = escape_format_chars(user_message)
    safe_tavily_answer = escape_format_chars(tavily_answer)

    localization_prompt = prompts.QNA_LOCALIZATION_PROMPT.format(
        user_message=safe_user_message, tavily_answer=safe_tavily_answer
    )
    # Получаем user_id и chat_id для логирования
    user_id = (
        placeholder_message.from_user.id if placeholder_message.from_user else None
    )
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None

    # Используем системную инструкцию из chat_state
    system_instruction = prompts.compose_system_instruction(chat_state.system_prompt)

    # Используем health-aware роутинг
    final_answer, _ = await _get_ai_response_with_routing(
        preferred_model,
        [{"role": "user", "parts": [localization_prompt]}],
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
    )

    # Проверяем, является ли ответ ошибкой (используем универсальную функцию)
    if await handle_ai_response_error(final_answer, placeholder_message):
        return  # Ошибка обработана, выходим
    elif final_answer:
        # Успешный ответ
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        await send_long_message(
            placeholder_message, final_answer, reply_markup=reply_markup
        )
    else:
        # Пустой ответ
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")


async def _handle_research_agent(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: db.ChatState,
    model_override: Optional[str] = None,
    search_query: str = None,
):
    # Если передан search_query, используем его для поиска, а user_message для локализации
    actual_search_query = search_query if search_query else user_message

    await metrics_collector.record_search_query()

    try:
        await update_stage(placeholder_message, STAGES_SEARCH_DEEP, 0)
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        placeholder_message = await placeholder_message.reply_text(
            "🔎 Ищу источники..."
        )

    # Получаем user_id и chat_id для логирования
    user_id = (
        placeholder_message.from_user.id if placeholder_message.from_user else None
    )
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None

    try:
        search_result = await services.tavily_search_agent(
            actual_search_query, search_type="search", user_id=user_id, chat_id=chat_id
        )
    except Exception as search_error:
        logging.error(f"Error in Tavily search: {search_error}")
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при поиске. Попробуйте позже."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    if search_result.get("error"):
        try:
            await placeholder_message.edit_text(search_result["error"])
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    search_results = search_result.get("results", [])
    if not search_results:
        try:
            await placeholder_message.edit_text(
                "Не удалось найти релевантные источники для исследования."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    # Проверяем, что search_results содержит валидные данные
    valid_results = []
    if search_results:
        for result in search_results:
            if isinstance(result, dict) and result.get("url") and result.get("title"):
                valid_results.append(result)
            else:
                logging.warning(f"Skipping invalid search result: {result}")

    if not valid_results:
        try:
            await placeholder_message.edit_text(
                "Не удалось найти валидные источники для исследования."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    search_results = valid_results  # Используем только валидные результаты

    try:
        count = len(search_results) if search_results else 0
        await placeholder_message.edit_text(
            f"✅ Найдено {count} источников. Выбираю лучшие..."
        )
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")

    # Используем модель из chat_state, если она указана, иначе используем настройки по умолчанию
    preferred_model = (
        chat_state.model
        if chat_state.model
        else (
            settings.OPENROUTER_URL_SELECTION_MODEL
            if get_openrouter_keys()
            else settings.URL_SELECTION_MODEL
        )
    )

    try:
        # Безопасная сериализация search_results
        safe_search_results = []
        if search_results:
            for result in search_results:
                safe_result = {
                    "title": str(result.get("title", "")),
                    "url": str(result.get("url", "")),
                    "content": str(result.get("content", "")),
                    "score": float(result.get("score", 0.0))
                    if result.get("score") is not None
                    else 0.0,
                }
                safe_search_results.append(safe_result)

        # Экранируем фигурные скобки в user_message для предотвращения ошибок форматирования
        safe_user_message = escape_format_chars(user_message)

        selection_prompt = prompts.URL_SELECTION_PROMPT.format(
            user_message=safe_user_message,
            # Optimized: Removed indent=2 to save tokens and improve performance
            search_results_json=await asyncio.to_thread(
                lambda: json.dumps(safe_search_results, ensure_ascii=False)
            ),
        )

        # Создаем parts для API: промпт
        parts = [selection_prompt] if selection_prompt else []
        # Используем системную инструкцию из chat_state
        system_instruction = prompts.compose_system_instruction(
            chat_state.system_prompt
        )

        # Используем health-aware роутинг
        selected_urls_str, _ = await _get_ai_response_with_routing(
            preferred_model,
            [{"role": "user", "parts": parts}],
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
        )

        # Проверяем, является ли ответ ошибкой
        from app.errors import (
            is_error_message,
            is_retryable_error,
            build_retry_and_roles_keyboard,
        )

        if selected_urls_str and is_error_message(selected_urls_str):
            # Это ошибка - показываем с кнопкой повтора
            if is_retryable_error(selected_urls_str):
                reply_markup = build_retry_and_roles_keyboard()
            else:
                from app.errors import build_roles_keyboard

                reply_markup = build_roles_keyboard()

            try:
                await placeholder_message.edit_text(
                    selected_urls_str, reply_markup=reply_markup
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return

        if not selected_urls_str:
            try:
                await placeholder_message.edit_text(
                    "Не удалось выбрать источники.",
                    reply_markup=build_retry_and_roles_keyboard(),
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return
    except Exception as gemini_error:
        logging.error(f"Error in Gemini URL selection: {gemini_error}")
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при выборе источников. Попробуйте позже."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    selected_urls = [
        url.strip()
        for url in selected_urls_str.split(",")
        if url.strip().startswith("http")
    ]

    if not selected_urls:
        try:
            await placeholder_message.edit_text(
                "Не удалось выбрать подходящие источники для глубокого анализа. Попробуйте переформулировать запрос."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        count = len(selected_urls) if selected_urls else 0
        await placeholder_message.edit_text(
            f"✅ Выбрано {count} источников. Собираю контент..."
        )
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")

    final_context_list = []
    if selected_urls and search_results:
        # Создаем словарь для быстрого поиска по URL (O(1) вместо O(N*M))
        results_map = {res.get("url"): res for res in search_results if res.get("url")}
        for url in selected_urls:
            res = results_map.get(url)
            if res:
                # Возвращаем старый формат для совместимости с Gemini API
                # но с улучшенной структурой для AI
                source_info = (
                    f"Источник: {res.get('url')}\nСодержание:\n{res.get('content')}"
                )
                final_context_list.append(source_info)

    full_context = "\n\n---\n\n".join(final_context_list) if final_context_list else ""

    if not full_context:
        try:
            await placeholder_message.edit_text(
                "Не удалось собрать контент с выбранных страниц."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        await update_stage(placeholder_message, STAGES_SEARCH_DEEP, 2)
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")

    # Используем модель из chat_state или переопределение, если указано
    model_for_synthesis = (
        model_override
        or chat_state.model
        or (
            settings.OPENROUTER_RESEARCH_MODEL
            if get_openrouter_keys()
            else settings.RESEARCH_MODEL
        )
    )

    # Экранируем фигурные скобки в данных для предотвращения ошибок форматирования
    # Применяем экранирование к данным перед форматированием промпта
    safe_full_context = escape_format_chars(full_context)
    safe_user_message = escape_format_chars(user_message)

    augmented_prompt = prompts.SYNTHESIS_PROMPT.format(
        full_context=safe_full_context, user_message=safe_user_message
    )
    # Подготавливаем контекст с учётом лимитов токенов

    # Извлекаем суммаризацию из истории, если есть
    summary = None
    if (
        chat_state.history
        and isinstance(chat_state.history, list)
        and len(chat_state.history) > 0
    ):
        # Проверяем, есть ли суммаризация в первом сообщении
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
            # Убираем суммаризацию из истории для обработки
            chat_state.history = chat_state.history[1:]

    # Добавляем augmented_prompt в историю
    chat_state.history.append({"role": "user", "parts": [augmented_prompt]})

    # Подготавливаем контекст с лимитами
    prepared_history, new_summary = prompts.prepare_context_with_limits(
        chat_state.history, "", summary
    )

    # Строим финальный контекст
    final_context = prompts.build_context_with_summary(
        prepared_history, new_summary, ""
    )

    # Обновляем историю в chat_state
    chat_state.history = final_context

    try:
        # Проверяем, что history не пустой
        if not chat_state.history or len(chat_state.history) == 0:
            try:
                await placeholder_message.edit_text(
                    "❌ История чата пуста. Невозможно обработать вопрос."
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return

        # Используем health-aware роутинг
        response_text, new_token_count = await _get_ai_response_with_routing(
            model_for_synthesis,
            chat_state.history,
            system_instruction=prompts.compose_system_instruction(
                chat_state.system_prompt
            ),
            user_id=user_id,
            chat_id=chat_id,
        )
    except Exception as ai_error:
        logging.error(f"Error in AI synthesis: {ai_error}")
        chat_state.history.pop()  # Убираем добавленный промпт
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text(
                "❌ Произошла ошибка при синтезе ответа. Попробуйте позже."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    if response_text and response_text.strip():
        # Проверяем, является ли ответ ошибкой
        from app.errors import (
            is_error_message,
            is_retryable_error,
            build_retry_and_roles_keyboard,
        )

        # Используем универсальную функцию обработки ошибок
        async def cleanup_on_error():
            chat_state.history.pop()  # Убираем добавленный промпт
            await db.update_user_chat(user_id, chat_state)

        if await handle_ai_response_error(
            response_text, placeholder_message, on_error_callback=cleanup_on_error
        ):
            return  # Ошибка обработана, выходим
        else:
            # Успешный ответ
            await send_long_message(
                placeholder_message, response_text, is_deep_dive=True
            )
            chat_state.history.append({"role": "model", "parts": [response_text]})
            chat_state.token_count = new_token_count
            chat_state.is_deep_dive = True

            # Генерируем уникальный thread_id для deep dive сессии
            import uuid

            # Безопасная проверка атрибута deep_dive_thread_id
            if (
                not hasattr(chat_state, "deep_dive_thread_id")
                or not chat_state.deep_dive_thread_id
            ):
                chat_state.deep_dive_thread_id = str(uuid.uuid4())
                logging.info(
                    f"Generated deep dive thread_id {chat_state.deep_dive_thread_id} for user {user_id}"
                )

            await db.update_user_chat(user_id, chat_state)
            logging.info(
                f"Deep dive mode activated for user {user_id} with thread_id {chat_state.deep_dive_thread_id}"
            )
    else:
        chat_state.history.pop()
        await db.update_user_chat(user_id, chat_state)
        logging.warning(
            f"Empty response from Gemini API for deep dive synthesis by user {user_id}"
        )
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")


async def _handle_complex_agent_search(
    placeholder_message: Message, original_message: Message, search_prefix: str
):
    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Используем `from_user.id`, а не `effective_user.id`
    user_id = original_message.from_user.id

    try:
        await placeholder_message.edit_text("🖼️ Анализирую изображение...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text(
            "🖼️ Анализирую изображение..."
        )

    vision_model = settings.RESEARCH_MODEL

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(photo_data))

    analysis_prompt = prompts.IMAGE_ANALYSIS_PROMPT

    # Создаем parts для Gemini API: промпт + изображение
    parts = [analysis_prompt, img] if img else [analysis_prompt]
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    search_query, _ = await _get_ai_response_with_routing(
        vision_model,
        [{"role": "user", "parts": parts}],
        user_id=user_id,
        chat_id=chat_id,
    )

    # Проверяем ошибки от роутера
    if await handle_ai_response_error(search_query, placeholder_message):
        return

    if not search_query:
        try:
            await placeholder_message.edit_text(
                "Не удалось проанализировать изображение для поиска."
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    chat_state = await db.get_user_chat(user_id)
    # Получаем оригинальное сообщение пользователя для локализации
    original_user_message = original_message.caption or "Опиши это изображение."

    if search_prefix == "?":
        await _handle_qna_search(
            placeholder_message, original_user_message, chat_state, search_query
        )
    else:
        await _handle_research_agent(
            placeholder_message,
            user_id,
            original_user_message,
            chat_state,
            search_query=search_query,
        )
