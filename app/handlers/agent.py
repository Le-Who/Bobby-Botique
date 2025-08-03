import logging
import json
import io
from PIL import Image
from typing import Optional
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import settings
from .. import database as db
from .. import services
from ..utils.messaging import send_long_message
from .. import state
from .. import prompts

async def _resolve_gemini_request(preferred_model: str):
    key = await db.get_available_gemini_key(preferred_model)
    if key:
        return key, preferred_model, None

    logging.warning(f"All keys for preferred model {preferred_model} are exhausted. Attempting fallback.")
    
    fallback_priority = [settings.RESEARCH_MODEL, settings.DEFAULT_MODEL, settings.QNA_MODEL]
    for fallback_model in fallback_priority:
        if fallback_model == preferred_model:
            continue
        key = await db.get_available_gemini_key(fallback_model)
        if key:
            logging.info(f"Found available fallback key for model {fallback_model}.")
            return key, fallback_model, "confirm_fallback"
            
    logging.error("All Gemini API keys for all models are exhausted.")
    return None, None, "all_exhausted"

async def _handle_qna_search(placeholder_message: Message, user_message: str, chat_state: db.ChatState):
    await placeholder_message.edit_text("🔎 Ищу быстрый ответ...")
    search_result = await services.tavily_search_agent(user_message, search_type='qna')
    if search_result.get("error"):
        await placeholder_message.edit_text(search_result["error"])
        return

    tavily_answer = search_result.get("content", "Не удалось найти прямой ответ.")
    await placeholder_message.edit_text("🌍 Адаптирую ответ...")
    
    gemini_key, model_used, _ = await _resolve_gemini_request(settings.QNA_MODEL)
    if not gemini_key:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {settings.QNA_MODEL} закончились.")
        return

    localization_prompt = prompts.QNA_LOCALIZATION_PROMPT.format(
        user_message=user_message, tavily_answer=tavily_answer
    )
    final_answer, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [localization_prompt]}], model_used)
    
    await send_long_message(placeholder_message, final_answer)
    await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)

async def _handle_research_agent(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState, model_override: Optional[str] = None):
    await placeholder_message.edit_text("🔎 Ищу источники...")
    search_result = await services.tavily_search_agent(user_message, search_type='search')
    if search_result.get("error"):
        await placeholder_message.edit_text(search_result["error"])
        return
    
    search_results = search_result.get('results', [])
    if not search_results:
        await placeholder_message.edit_text("Не удалось найти релевантные источники для исследования.")
        return

    await placeholder_message.edit_text(f"✅ Найдено {len(search_results)} источников. Выбираю лучшие...")
    gemini_key, model_used, _ = await _resolve_gemini_request(settings.URL_SELECTION_MODEL)
    if not gemini_key:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {settings.URL_SELECTION_MODEL} (выбор URL) закончились.")
        return

    selection_prompt = prompts.URL_SELECTION_PROMPT.format(
        user_message=user_message,
        search_results_json=json.dumps(search_results, indent=2, ensure_ascii=False)
    )
    
    selected_urls_str, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [selection_prompt]}], model_used)
    await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
    selected_urls = [url.strip() for url in selected_urls_str.split(',') if url.strip().startswith('http')]

    if not selected_urls:
        await placeholder_message.edit_text("Не удалось выбрать подходящие источники для глубокого анализа. Попробуйте переформулировать запрос.")
        return

    await placeholder_message.edit_text(f"✅ Выбрано {len(selected_urls)} источников. Собираю контент...")
    
    final_context_list = [f"Источник (URL: {res.get('url')}):\n{res.get('content')}" for url in selected_urls for res in search_results if res.get('url') == url]
    full_context = "\n\n---\n\n".join(final_context_list)

    if not full_context:
        await placeholder_message.edit_text("Не удалось собрать контент с выбранных страниц.")
        return

    await placeholder_message.edit_text(f"🧠 Синтезирую ответ на основе {len(full_context)} символов...")
    model_for_synthesis = model_override or settings.RESEARCH_MODEL
    gemini_key, model_used, _ = await _resolve_gemini_request(model_for_synthesis)
    if not gemini_key:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {model_for_synthesis} закончились.")
        return
        
    augmented_prompt = prompts.SYNTHESIS_PROMPT.format(
        full_context=full_context, user_message=user_message
    )
    chat_state.history.append({'role': 'user', 'parts': [augmented_prompt]})
    
    response_text, new_token_count = await services.get_gemini_response(gemini_key['api_key'], chat_state.history, model_used, system_instruction=chat_state.system_prompt)
    
    if response_text:
        await send_long_message(placeholder_message, response_text)
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        chat_state.history.append({'role': 'model', 'parts': [response_text]})
        chat_state.token_count = new_token_count
        await db.update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()
        await db.update_user_chat(user_id, chat_state)
        await placeholder_message.edit_text("Получен пустой ответ от API.")

async def _handle_regular_chat(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState, model_override: Optional[str] = None):
    model_for_this_request = model_override or chat_state.model
    gemini_key, model_used, resolution = await _resolve_gemini_request(model_for_this_request)

    if resolution == "all_exhausted":
        await placeholder_message.edit_text("🚫 Все лимиты для всех моделей Gemini на сегодня исчерпаны. Попробуйте позже.")
        return
    
    if resolution == "confirm_fallback":
        keyboard = [
            [InlineKeyboardButton(f"Да, использовать {model_used}", callback_data=f"fallback:confirm:{model_used}")],
            [InlineKeyboardButton("Нет, отмена", callback_data="fallback:cancel")]
        ]
        await placeholder_message.edit_text(
            f"Все лимиты для модели `{model_for_this_request}` на сегодня исчерпаны.\n"
            f"Однако, я могу выполнить ваш запрос, используя `{model_used}`. Качество ответа может быть другим.\n"
            "Продолжить?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    chat_state.history.append({'role': 'user', 'parts': [user_message]})
    await placeholder_message.edit_text(f"🧠 Модель {model_used} думает...")
    response_text, new_token_count = await services.get_gemini_response(gemini_key['api_key'], chat_state.history, model_used, system_instruction=chat_state.system_prompt)
    
    if response_text:
        await send_long_message(placeholder_message, response_text)
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        chat_state.history.append({'role': 'model', 'parts': [response_text]})
        chat_state.token_count = new_token_count
        await db.update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()
        await db.update_user_chat(user_id, chat_state)
        await placeholder_message.edit_text("Получен пустой ответ от API.")

async def _handle_photo(placeholder_message: Message, original_message: Message, chat_state: db.ChatState):
    gemini_key, model_used, resolution = await _resolve_gemini_request(chat_state.model)
    if resolution:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {chat_state.model} для обработки фото закончились.")
        return

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    img = Image.open(io.BytesIO(photo_data))
    prompt = original_message.caption or "Опиши это изображение."
    response_text, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [prompt, img]}], model_used)
    
    await send_long_message(placeholder_message, response_text or "Не удалось обработать изображение.")
    await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)

async def _handle_complex_agent_search(placeholder_message: Message, original_message: Message, search_prefix: str):
    user_id = original_message.effective_user.id
    
    await placeholder_message.edit_text("🖼️ Анализирую изображение...")
    vision_model = settings.RESEARCH_MODEL
    gemini_key, _, resolution = await _resolve_gemini_request(vision_model)
    if resolution:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {vision_model} (анализ фото) закончились.")
        return

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    img = Image.open(io.BytesIO(photo_data))
    
    analysis_prompt = prompts.IMAGE_ANALYSIS_PROMPT
    
    search_query, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [analysis_prompt, img]}], vision_model)
    await db.increment_gemini_key_usage(gemini_key['key_hash'], vision_model)

    if not search_query:
        await placeholder_message.edit_text("Не удалось проанализировать изображение для поиска.")
        return

    chat_state = await db.get_user_chat(user_id)
    if search_prefix == '?':
        await _handle_qna_search(placeholder_message, search_query, chat_state)
    else:
        await _handle_research_agent(placeholder_message, user_id, search_query, chat_state)

async def process_long_request(placeholder_message: Message, update: Update, context):
    try:
        is_photo = bool(update.message.photo)
        text = update.message.text or update.message.caption or ""
        chat_state = await db.get_user_chat(update.effective_user.id)

        if is_photo and (text.startswith('?') or text.startswith('??')):
            keyboard = [
                [InlineKeyboardButton("🖼️ Только описать фото", callback_data="complex:vision_only")],
                [InlineKeyboardButton("🔎 Выполнить сложный поиск", callback_data="complex:confirm")],
                [InlineKeyboardButton("❌ Отмена", callback_data="complex:cancel")]
            ]
            
            # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
            # Удаляем временный плейсхолдер и отправляем новый, но как ответ.
            # Это гарантирует сохранение reply_to_message.
            await placeholder_message.delete()
            await update.message.reply_text(
                "Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if is_photo:
            await _handle_photo(placeholder_message, update.message, chat_state)
            return

        if text.startswith('??'):
            await _handle_research_agent(placeholder_message, update.effective_user.id, text[2:].strip(), chat_state)
        elif text.startswith('?'):
            await _handle_qna_search(placeholder_message, text[1:].strip(), chat_state)
        elif chat_state.search_enabled:
            await _handle_research_agent(placeholder_message, update.effective_user.id, text, chat_state)
        else:
            await _handle_regular_chat(placeholder_message, update.effective_user.id, text, chat_state)

    except Exception as e:
        logging.error(f"Error in background task dispatcher: {e}", exc_info=True)
        try:
            await placeholder_message.edit_text(f"Произошла критическая ошибка: {e}")
        except Exception as inner_e:
            logging.error(f"Could not edit placeholder message: {inner_e}")
