import logging
import json
import io
from PIL import Image
from typing import Optional
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup

from .. import config
from .. import database as db
from .. import services
from ..utils.messaging import send_long_message
from .. import state

async def _resolve_gemini_request(preferred_model: str):
    key = await db.get_available_gemini_key(preferred_model)
    if key:
        return key, preferred_model, None

    logging.warning(f"All keys for preferred model {preferred_model} are exhausted. Attempting fallback.")
    
    fallback_priority = [config.RESEARCH_MODEL, config.DEFAULT_MODEL, config.QNA_MODEL]
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
    
    gemini_key, model_used, _ = await _resolve_gemini_request(config.QNA_MODEL)
    if not gemini_key:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {config.QNA_MODEL} закончились.")
        return

    localization_prompt = (
        "**TASK:** You are a localization and formatting assistant. Your job is to present a piece of information in the user's language.\n\n"
        f"**USER'S ORIGINAL QUERY:** \"{user_message}\"\n"
        f"**INFORMATION FOUND:** \"{tavily_answer}\"\n\n"
        "**INSTRUCTIONS:**\n"
        "1. Determine the language of the \"USER'S ORIGINAL QUERY\".\n"
        "2. Translate and present the \"INFORMATION FOUND\" in that language.\n"
        "3. Apply basic Telegram Markdown formatting if appropriate (e.g., making a key term bold).\n"
        "4. Your output MUST ONLY be the final, processed text. Do not add any conversational filler like \"Here is the answer...\" or \"According to the information...\"."
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
    gemini_key, model_used, _ = await _resolve_gemini_request(config.URL_SELECTION_MODEL)
    if not gemini_key:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {config.URL_SELECTION_MODEL} (выбор URL) закончились.")
        return

    selection_prompt = (
        "**ROLE:** You are an expert research analyst. Your task is to select the most relevant and authoritative web sources.\n\n"
        f"**USER QUERY:** \"{user_message}\"\n\n"
        "**TASK:** From the provided list of search results, select the TOP 2-5 URLs that are most likely to contain a detailed and direct answer to the user's query.\n\n"
        "**CRITERIA:**\n"
        "1. **Relevance:** The title and snippet must directly relate to the user's query.\n"
        "2. **Authority:** Prefer well-known news sites, official documentation, tech reviews, or established community resources. Avoid forums or personal blogs if better options exist.\n"
        "3. **Content-Rich:** Choose sources that promise detailed information (reviews, guides, specs) over simple mentions.\n\n"
        "**OUTPUT FORMAT:** Return ONLY a comma-separated list of the chosen URLs. Do not include any explanation, preamble, or formatting.\n\n"
        f"**SEARCH RESULTS FOR ANALYSIS:**\n{json.dumps(search_results, indent=2, ensure_ascii=False)}"
    )
    
    selected_urls_str, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [selection_prompt]}], model_used)
    await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
    selected_urls = [url.strip() for url in selected_urls_str.split(',') if url.strip().startswith('http')]

    if not selected_urls:
        await placeholder_message.edit_text("Не удалось выбрать подходящие источники для глубокого анализа. Попробуйте переформулировать запрос.")
        return

    await placeholder_message.edit_text(f"✅ Выбрано {len(selected_urls)} источников. Собираю контент...")
    
    final_context_list = []
    for url in selected_urls:
        for result in search_results:
            if result.get('url') == url:
                final_context_list.append(f"Источник (URL: {result.get('url')}):\n{result.get('content')}")
                break
    
    full_context = "\n\n---\n\n".join(final_context_list)

    if not full_context:
        await placeholder_message.edit_text("Не удалось собрать контент с выбранных страниц.")
        return

    await placeholder_message.edit_text(f"🧠 Синтезирую ответ на основе {len(full_context)} символов...")
    model_for_synthesis = model_override or config.RESEARCH_MODEL
    gemini_key, model_used, _ = await _resolve_gemini_request(model_for_synthesis)
    if not gemini_key:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {model_for_synthesis} закончились.")
        return
        
    augmented_prompt = (
        "**ROLE:** You are a helpful AI research assistant. Your goal is to provide a comprehensive, well-structured, and easy-to-read answer based *exclusively* on the provided context.\n\n"
        "**IMPORTANT CONTEXT RULE:** The following context is raw text scraped from the web. It may contain formatting errors. Your primary task is to extract the factual information, ignoring any broken formatting within the context itself.\n\n"
        f"**CONTEXT FROM WEB SEARCH:**\n---\n{full_context}\n---\n\n"
        f"**USER'S ORIGINAL QUERY:** \"{user_message}\"\n\n"
        "**FINAL TASK & RULES:**\n"
        "1. Synthesize the information from the raw context to fully answer the user's query.\n"
        "2. Structure your answer clearly using Telegram's MarkdownV2 syntax:\n"
        "   - For bold text, use `*bold text*`.\n"
        "   - For italic text, use `_italic text_`.\n"
        "   - For lists, each item must start with a hyphen (`- `).\n"
        "3. **You MUST cite your sources using the correct MarkdownV2 link format ONLY:** `[display text](URL)`.\n"
        "   - **CRITICAL:** You MUST NOT use any other link format, such as `[[...]]`. This is a strict requirement.\n"
        "   - The `[display text]` should be short and descriptive (e.g., the article title or `Источник 1`).\n"
        "   - The `(URL)` MUST be the full, original URL from the context.\n"
        "   - Any special characters (`.`, `!`, `-`) inside the `[display text]` part MUST be escaped with a preceding backslash (`\\`).\n"
        "4. If you find conflicting information, highlight this discrepancy.\n"
        "5. If the context is insufficient, state that clearly. Do not use any prior knowledge.\n\n"
        "**PERFECT CITATION EXAMPLE:**\n"
        "The price was listed as 5500 грн [according to this OLX listing](https://www.olx.ua/...).\n\n"
        "**BAD CITATION EXAMPLE (DO NOT USE):**\n"
        "The price was listed as 5500 грн [[OLX]](https://www.olx.ua/...)."
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
    vision_model = config.RESEARCH_MODEL
    gemini_key, _, resolution = await _resolve_gemini_request(vision_model)
    if resolution:
        await placeholder_message.edit_text(f"🚫 Ключи для модели {vision_model} (анализ фото) закончились.")
        return

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    img = Image.open(io.BytesIO(photo_data))
    
    analysis_prompt = (
        "**ROLE:** You are an image-to-text recognition engine for a web search pipeline. Your only function is to identify the main subject of an image and output a concise search query.\n\n"
        "**TASK:** Analyze the image and output a short, factual search query describing the main subject.\n\n"
        "**RULES:**\n"
        "- Be specific. If it's a landmark, name it (e.g., \"Eiffel Tower\"). If it's an object, name it (e.g., \"red 2023 Ferrari SF90 Stradale\").\n"
        "- Your output MUST be ONLY the search query text.\n"
        "- DO NOT add any conversational text, explanations, or preambles like \"The image shows...\" or \"Search query:\"."
    )
    
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
    user_id = update.effective_user.id
    try:
        is_photo = bool(update.message.photo)
        text = update.message.text or update.message.caption or ""
        chat_state = await db.get_user_chat(user_id)

        if is_photo and (text.startswith('?') or text.startswith('??')):
            keyboard = [
                [InlineKeyboardButton("🖼️ Только описать фото", callback_data="complex:vision_only")],
                [InlineKeyboardButton("🔎 Выполнить сложный поиск", callback_data="complex:confirm")],
                [InlineKeyboardButton("❌ Отмена", callback_data="complex:cancel")]
            ]
            await placeholder_message.edit_text(
                "Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if is_photo:
            await _handle_photo(placeholder_message, update.message, chat_state)
            return

        if text.startswith('??'):
            await _handle_research_agent(placeholder_message, user_id, text[2:].strip(), chat_state)
        elif text.startswith('?'):
            await _handle_qna_search(placeholder_message, text[1:].strip(), chat_state)
        elif chat_state.search_enabled:
            await _handle_research_agent(placeholder_message, user_id, text, chat_state)
        else:
            await _handle_regular_chat(placeholder_message, user_id, text, chat_state)

    except Exception as e:
        logging.error(f"Error in background task dispatcher: {e}", exc_info=True)
        try:
            await placeholder_message.edit_text(f"Произошла критическая ошибка: {e}")
        except Exception as inner_e:
            logging.error(f"Could not edit placeholder message: {inner_e}")
    finally:
        if user_id in state.ACTIVE_USER_TASKS:
            del state.ACTIVE_USER_TASKS[user_id]
