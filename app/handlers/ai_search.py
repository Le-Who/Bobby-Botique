"""
AI Search handlers — QnA quick answers, research agent, and complex agent search.
"""

import asyncio
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, NetworkError

from app import search_services
from app.config import get_openrouter_keys, settings
from app.core.agentic import AgenticSearch
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    handle_ai_response_error,
)
from app.metrics import metrics_collector, track_metrics
from app.prompt_registry import get_registry
from app.repos.chats import get_user_chat, update_user_chat
from app.utils.formatting import escape_format_chars
from app.utils.heartbeat import stop_heartbeat
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import (
    STAGES_SEARCH_DEEP,
    STAGES_SEARCH_QUICK,
    update_stage,
)
from app.utils.waiting_facts import get_waiting_message


@track_metrics("qna_search")
async def _handle_qna_search(
    placeholder_message: Message,
    user_message: str,
    chat_state: ChatState,
    search_query: str | None = None,
):
    # If beforeан search_query, use его for searchа, а user_message for локалfromации
    actual_search_query = search_query if search_query else user_message
    # chat_state используется for совместимости с другими функциями

    stop_heartbeat(placeholder_message.message_id)
    await metrics_collector.record_search_query()

    try:
        await update_stage(placeholder_message, STAGES_SEARCH_QUICK, 0)
    except (BadRequest, NetworkError) as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text("🔎 Ищу быстрый ответ...")

    # Get user_id и chat_id for логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None

    search_result = await search_services.tavily_search_agent(
        actual_search_query, search_type="qna", user_id=user_id, chat_id=chat_id
    )
    if search_result.get("error"):
        try:
            await placeholder_message.edit_text(search_result["error"])
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    tavily_answer = search_result.get("answer", "Не удалось найти прямой ответ.")
    try:
        await update_stage(placeholder_message, STAGES_SEARCH_QUICK, 1)
    except (BadRequest, NetworkError) as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text("🌍 Адаптирую ответ...")

    # Используем model from chat_state, if она указана, иначе use settings by default
    preferred_model = (
        chat_state.model
        if chat_state.model
        else (settings.OPENROUTER_QNA_MODEL if get_openrouter_keys() else settings.QNA_MODEL)
    )

    # Экранируем фигурные скобки в данных for предотвращения ошибок форматирования
    safe_user_message = escape_format_chars(user_message)
    safe_tavily_answer = escape_format_chars(tavily_answer)

    localization_prompt = get_registry().get_task_prompt(
        "qna_localization",
        user_message=safe_user_message,
        tavily_answer=safe_tavily_answer,
    )
    # Get user_id и chat_id for логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None

    from app.handlers.ai_core import _resolve_ai_request
    from app.streaming import stream_and_display

    _, model_used, _ = await _resolve_ai_request(preferred_model)
    history = [{"role": "user", "parts": [localization_prompt]}]
    system_instruction = get_registry().compose_system_prompt(role_prompt=chat_state.system_prompt)

    final_answer, success, stream_last_msg = await stream_and_display(
        placeholder_message,
        model_name=model_used,
        history=history,
        system_instruction=system_instruction,
        thinking_level=chat_state.thinking_level,
        user_id=user_id,
        bot=placeholder_message.get_bot(),
        chat_id=chat_id or 0,
        chat_type=(placeholder_message.chat.type if placeholder_message.chat else "private"),
    )

    streamed = bool(success and final_answer)

    if not streamed:
        final_answer, _ = await _get_ai_response_with_routing(
            model_used,
            history,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
        )

    # Check, является ли response ошибкой (use универсальную функцию)
    if await handle_ai_response_error(final_answer, placeholder_message):
        return  # Error обработана, выходим
    elif final_answer:
        # Успешный response
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        if not streamed:
            await send_long_message(placeholder_message, final_answer, reply_markup=reply_markup)
        else:
            button_msg = stream_last_msg if stream_last_msg else placeholder_message
            try:
                await button_msg.edit_reply_markup(reply_markup=reply_markup)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logging.warning("Final button edit failed: %s", e)
    else:
        # Пустой response
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


@track_metrics("research_search")
async def _handle_research_agent(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
    model_override: str | None = None,
    search_query: str | None = None,
):
    # If search_query is provided, use it for search, else use user_message
    actual_search_query = search_query if search_query else user_message

    stop_heartbeat(placeholder_message.message_id)
    await metrics_collector.record_search_query()

    # Determine trace IDs
    trace_user_id: int | None = placeholder_message.from_user.id if placeholder_message.from_user else None
    trace_chat_id: int | None = placeholder_message.chat.id if placeholder_message.chat else None

    # Get the key and model for AgenticSearch. Fallback to default if no OpenRouter/override
    from app.repos.keys import get_available_gemini_key

    model_used = model_override or chat_state.model or settings.AGENTIC_MODEL or settings.RESEARCH_MODEL
    key_data = await get_available_gemini_key(model_used)
    if not key_data:
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "❌ Нет доступных ключей API. Пожалуйста, попробуйте позже.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except (BadRequest, NetworkError):
            pass
        return

    agent = AgenticSearch(model_name=model_used, api_key=key_data["api_key"])

    # Define the status callback that will show stages + fun facts
    last_fact_time = 0.0
    current_fact = ""

    async def on_status(stage_text: str):
        nonlocal last_fact_time, current_fact
        now = time.monotonic()
        if now - last_fact_time > 10.0 or not current_fact:
            current_fact = await get_waiting_message(trace_user_id)
            last_fact_time = now

        full_text = f"{stage_text}\n\n{current_fact}"
        try:
            await placeholder_message.edit_text(full_text)
        except (BadRequest, NetworkError) as edit_error:
            if "not modified" not in str(edit_error).lower():
                logging.error("Could not edit placeholder message with status: %s", edit_error)

    # Run the agentic loop
    try:
        final_answer = await agent.run(
            query=actual_search_query,
            on_status=on_status,
            user_id=trace_user_id,
            chat_id=trace_chat_id,
            history=chat_state.history if chat_state.history else None,
        )
    except Exception as ai_error:
        logging.error("Error in AgenticSearch run: %s", ai_error, exc_info=True)
        try:
            await placeholder_message.edit_text(
                "❌ Внутренняя ошибка при проведении глубокого исследования. Попробуйте отформатировать запрос иначе."
            )
        except (BadRequest, NetworkError):
            pass
        return

    # Process and display the final answer
    if final_answer and not final_answer.startswith("❌"):
        # We successfully got an answer
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="deepdive:new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        await send_long_message(
            placeholder_message,
            final_answer,
            reply_markup=reply_markup,
            is_deep_dive=True,
        )

        # Save to history
        chat_state.history.append({"role": "user", "parts": [actual_search_query]})
        chat_state.history.append({"role": "model", "parts": [final_answer]})
        chat_state.is_deep_dive = True

        # Generate unique thread_id for deep dive session if absent
        import uuid

        if not hasattr(chat_state, "deep_dive_thread_id") or not chat_state.deep_dive_thread_id:
            chat_state.deep_dive_thread_id = str(uuid.uuid4())
            logging.info(
                "Generated deep dive thread_id %s for user %s",
                chat_state.deep_dive_thread_id,
                user_id,
            )

        await update_user_chat(user_id, chat_state)
    else:
        # Agent failed to get an answer or returned an explicit error
        error_msg = final_answer if final_answer else "Не удалось сформировать ответ."
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                error_msg,
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


@track_metrics("complex_search")
async def _handle_complex_agent_search(placeholder_message: Message, original_message: Message, search_prefix: str):
    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Используем `from_user.id`, а не `effective_user.id`
    user_id = original_message.from_user.id

    try:
        await placeholder_message.edit_text("🖼️ Анализирую изображение...")
    except (BadRequest, NetworkError) as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        # If не можем отредактировать, отправляем new message
        placeholder_message = await placeholder_message.reply_text("🖼️ Анализирую изображение...")

    vision_model = settings.RESEARCH_MODEL

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(photo_data))

    analysis_prompt = get_registry().get("image_analysis").text

    # Create parts for Gemini API: промпт + image
    parts = [analysis_prompt, img] if img else [analysis_prompt]
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    search_query, _ = await _get_ai_response_with_routing(
        vision_model,
        [{"role": "user", "parts": parts}],
        user_id=user_id,
        chat_id=chat_id,
    )

    # Check ошибки от роутера
    if await handle_ai_response_error(search_query, placeholder_message):
        return

    if not search_query:
        try:
            await placeholder_message.edit_text("Не удалось проанализировать изображение для поиска.")
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    chat_state = await get_user_chat(user_id)
    # Get оригинальное message user for локалfromации
    original_user_message = original_message.caption or "Опиши это изображение."

    if search_prefix == "?":
        await _handle_qna_search(placeholder_message, original_user_message, chat_state, search_query)
    else:
        await _handle_research_agent(
            placeholder_message,
            user_id,
            original_user_message,
            chat_state,
            search_query=search_query,
        )
