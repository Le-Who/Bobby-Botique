import logging
import json
import io
from PIL import Image
from typing import Optional
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..config import settings
from .. import database as db
from .. import services
from ..utils.messaging import send_long_message
from .. import state
from .. import prompts
from ..metrics import metrics_collector
from ..utils.api_logger import api_logger

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

async def _handle_qna_search(placeholder_message: Message, user_message: str, chat_state: db.ChatState, search_query: str = None):
    # Если передан search_query, используем его для поиска, а user_message для локализации
    actual_search_query = search_query if search_query else user_message
    
    await metrics_collector.record_search_query()
    
    try:
        await placeholder_message.edit_text("🔎 Ищу быстрый ответ...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text("🔎 Ищу быстрый ответ...")
    
    # Получаем user_id и chat_id для логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    
    search_result = await services.tavily_search_agent(
        actual_search_query, 
        search_type='qna',
        user_id=user_id,
        chat_id=chat_id
    )
    if search_result.get("error"):
        try:
            await placeholder_message.edit_text(search_result["error"])
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    tavily_answer = search_result.get("answer", "Не удалось найти прямой ответ.")
    try:
        await placeholder_message.edit_text("🌍 Адаптирую ответ...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text("🌍 Адаптирую ответ...")
    
    gemini_key, model_used, _ = await _resolve_gemini_request(settings.QNA_MODEL)
    if not gemini_key:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {settings.QNA_MODEL} закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    localization_prompt = prompts.QNA_LOCALIZATION_PROMPT.format(
        user_message=user_message, tavily_answer=tavily_answer
    )
    # Получаем user_id и chat_id для логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    
    final_answer, _ = await services.get_gemini_response(
        gemini_key['api_key'], 
        [{'role': 'user', 'parts': [localization_prompt]}], 
        model_used,
        user_id=user_id,
        chat_id=chat_id
    )
    
    await send_long_message(placeholder_message, final_answer)
    await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)

async def _handle_research_agent(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState, search_query: str = None):
    """Обрабатывает запросы к исследовательскому агенту"""
    try:
        await placeholder_message.edit_text("🔍 Исследую тему...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text("🔍 Исследую тему...")
    
    # Получаем user_id и chat_id для логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    
    # Добавляем новый вопрос пользователя в историю
    chat_state.history.append({'role': 'user', 'parts': [user_message]})
    
    # Используем модель для исследований
    model_for_research = settings.RESEARCH_MODEL
    gemini_key, model_used, _ = await _resolve_gemini_request(model_for_research)
    if not gemini_key:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {model_for_research} закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    try:
        # Отправляем запрос с сохраненной историей и контекстом
        response_text, new_token_count = await services.get_gemini_response(
            gemini_key['api_key'], 
            chat_state.history, 
            model_used, 
            system_instruction=chat_state.system_prompt
        )
    except Exception as gemini_error:
        logging.error(f"Error in Gemini research agent: {gemini_error}")
        chat_state.history.pop()  # Убираем добавленный вопрос
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при исследовании. Попробуйте позже.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    if response_text:
        # Отправляем ответ с кнопками deep dive
        await send_long_message(placeholder_message, response_text, is_deep_dive=True)
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        
        # Добавляем ответ модели в историю
        chat_state.history.append({'role': 'model', 'parts': [response_text]})
        chat_state.token_count = new_token_count
        chat_state.is_deep_dive = True  # Устанавливаем флаг deep dive
        
        # Устанавливаем ID потока deep dive для отслеживания контекста
        if placeholder_message.message_id:
            chat_state.deep_dive_thread_id = placeholder_message.message_id
        else:
            # Fallback: используем текущее время как идентификатор потока
            import time
            chat_state.deep_dive_thread_id = int(time.time())
        
        await db.update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()  # Убираем вопрос, если ответ пустой
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text("Получен пустой ответ от API при исследовании.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")

async def _handle_document_question(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState):
    """Обрабатывает вопросы по загруженным документам"""
    try:
        # Получаем последний документ пользователя
        from ..document_processor import get_user_documents, get_document_content
        
        documents = await get_user_documents(user_id)
        if not documents:
            try:
                await placeholder_message.edit_text("❌ У вас нет загруженных документов. Сначала загрузите документ.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                await placeholder_message.reply_text("❌ У вас нет загруженных документов. Сначала загрузите документ.")
            return
        
        # Берем самый последний документ
        latest_document = documents[0]
        document_content = await get_document_content(latest_document['id'], user_id)
        
        if not document_content:
            try:
                await placeholder_message.edit_text("❌ Не удалось получить содержимое документа.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                await placeholder_message.reply_text("❌ Не удалось получить содержимое документа.")
            return
        
        try:
            await placeholder_message.edit_text("📄 Анализирую документ...")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Если не можем отредактировать, отправляем новое сообщение
            placeholder_message = await placeholder_message.reply_text("📄 Анализирую документ...")
        
        # Ограничиваем размер контекста документа
        max_context_length = 30000  # Ограничиваем до 30K символов
        original_length = len(document_content)
        if len(document_content) > max_context_length:
            document_content = document_content[:max_context_length] + "\n\n[Документ обрезан для экономии токенов]"
            logging.info(f"Document content truncated from {original_length} to {len(document_content)} characters")
        
        logging.info(f"Processing document question for user {user_id}, document: {latest_document['filename']}, content length: {len(document_content)}")
        
        # Создаем промпт для вопроса по документу
        document_prompt = f"""Ты - помощник для анализа документов. Пользователь задал вопрос по документу.

Содержимое документа:
{document_content}

Вопрос пользователя: {user_message}

**ИНСТРУКЦИИ ПО ФОРМАТИРОВАНИЮ:**
1. Используй Telegram MarkdownV2 синтаксис:
   - Для жирного текста: `*жирный текст*` (НЕ `**жирный текст**`)
   - Для курсива: `_курсив_` (НЕ `__курсив__`)
   - Для кода: `` `код` ``
   - Для списков: каждый элемент начинается с `- `

2. **КРИТИЧЕСКИЕ ПРАВИЛА ФОРМАТИРОВАНИЯ:**
   - НИКОГДА не используй HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, etc.
   - НИКОГДА не используй двойные звездочки `**текст**` - используй одинарные `*текст*`
   - НИКОГДА не используй двойные подчеркивания `__текст__` - используй одинарные `_текст_`
   - НИКОГДА не используй LaTeX математические выражения: `$...$` или `$$...$$`

3. **ФОРМАТИРОВАНИЕ МАТЕМАТИЧЕСКИХ ВЫРАЖЕНИЙ:**
   - НИКОГДА не используй LaTeX: `$1 \times 1 = 1$` или `$$\sqrt{2}$$`
   - ВСЕГДА используй обычный текст: `1 × 1 = 1` или `√2` или `корень из 2`
   - Для дробей: используй `/` (например, `1/2` вместо `$\frac{1}{2}$`)
   - Для корней: используй `√` или `корень из` (например, `√2` или `корень из 2`)
   - Для степеней: используй `^` (например, `2^2 = 4` вместо `$2^2 = 4$`)
   - Для умножения: используй `×` или `*` (например, `2 × 3 = 6` или `2 * 3 = 6`)

Ответь на вопрос пользователя, основываясь на содержимом документа. Если в документе нет информации для ответа, честно скажи об этом."""
        
        gemini_key, model_used, _ = await _resolve_gemini_request(settings.DEFAULT_MODEL)
        if not gemini_key:
            try:
                await placeholder_message.edit_text(f"🚫 Ключи для модели {settings.DEFAULT_MODEL} закончились.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                await placeholder_message.reply_text(f"🚫 Ключи для модели {settings.DEFAULT_MODEL} закончились.")
            return
        
        response_text, _ = await services.get_gemini_response(
            gemini_key['api_key'], 
            [{'role': 'user', 'parts': [document_prompt]}], 
            model_used
        )
        
        if response_text:
            # Создаем кнопки для управления документом
            keyboard = [
                [InlineKeyboardButton("📄 Загрузить другой документ", callback_data="doc:upload_new")],
                [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
                [InlineKeyboardButton("❌ Отменить работу с документами", callback_data="doc:cancel")]
            ]
            
            # Отправляем ответ с кнопками
            await send_long_message(placeholder_message, response_text, reply_markup=InlineKeyboardMarkup(keyboard))
            await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
            await metrics_collector.record_api_call("document_question", model_used)
        else:
            try:
                await placeholder_message.edit_text("❌ Не удалось получить ответ от AI.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Fallback на новое сообщение
                await placeholder_message.reply_text("❌ Не удалось получить ответ от AI.")
            
    except Exception as e:
        logging.error(f"Error processing document question: {e}", exc_info=True)
        try:
            await placeholder_message.edit_text(f"❌ Произошла ошибка при обработке вопроса по документу: {str(e)}")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Fallback на новое сообщение
            await placeholder_message.reply_text(f"❌ Произошла ошибка при обработке вопроса по документу: {str(e)}")

async def _handle_regular_chat(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState, model_override: Optional[str] = None):
    model_for_this_request = model_override or chat_state.model
    gemini_key, model_used, resolution = await _resolve_gemini_request(model_for_this_request)

    if resolution == "all_exhausted":
        try:
            await placeholder_message.edit_text("🚫 Все лимиты для всех моделей Gemini на сегодня исчерпаны. Попробуйте позже.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    if resolution == "confirm_fallback":
        keyboard = [
            [InlineKeyboardButton(f"Да, использовать {model_used}", callback_data=f"fallback:confirm:{model_used}")],
            [InlineKeyboardButton("Нет, отмена", callback_data="fallback:cancel")]
        ]
        try:
            await placeholder_message.edit_text(
                f"Все лимиты для модели `{model_for_this_request}` на сегодня исчерпаны.\n"
                f"Однако, я могу выполнить ваш запрос, используя `{model_used}`. Качество ответа может быть другим.\n"
                "Продолжить?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    chat_state.history.append({'role': 'user', 'parts': [user_message]})
    try:
        await placeholder_message.edit_text(f"🧠 Модель {model_used} думает...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text(f"🧠 Модель {model_used} думает...")
    
    # Используем системную инструкцию пользователя или инструкцию по умолчанию
    system_instruction = chat_state.system_prompt or settings.DEFAULT_SYSTEM_PROMPT
    response_text, new_token_count = await services.get_gemini_response(gemini_key['api_key'], chat_state.history, model_used, system_instruction=system_instruction)
    
    if response_text:
        reply_markup = None
        if chat_state.is_deep_dive:
            keyboard = [[InlineKeyboardButton("✨ Начать новую тему", callback_data="deepdive:new_topic")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Устанавливаем ID потока deep dive для отслеживания контекста
            if placeholder_message.message_id:
                chat_state.deep_dive_thread_id = placeholder_message.message_id
            else:
                # Fallback: используем текущее время как идентификатор потока
                import time
                chat_state.deep_dive_thread_id = int(time.time())

        await send_long_message(placeholder_message, response_text, reply_markup=reply_markup)
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        chat_state.history.append({'role': 'model', 'parts': [response_text]})
        chat_state.token_count = new_token_count
        await db.update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text("Получен пустой ответ от API.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")

async def _handle_photo(placeholder_message: Message, original_message: Message, chat_state: db.ChatState):
    gemini_key, model_used, resolution = await _resolve_gemini_request(chat_state.model)
    if resolution:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {chat_state.model} для обработки фото закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Fallback на новое сообщение
            await original_message.reply_text(f"🚫 Ключи для модели {chat_state.model} для обработки фото закончились.")
        return

    try:
        photo_file = await original_message.photo[-1].get_file()
        photo_data = await photo_file.download_as_bytearray()
        img = Image.open(io.BytesIO(photo_data))
        prompt = original_message.caption or "Опиши это изображение."
        
        # Добавляем инструкции по форматированию к промпту для изображений
        formatted_prompt = f"""{prompt}

**ВАЖНО:** Используй правильное форматирование для Telegram:
- Для жирного текста: `*жирный текст*` (НЕ `**жирный текст**`)
- Для курсива: `_курсив_` (НЕ `__курсив__`)
- Для кода: `` `код` ``
- НИКОГДА не используй HTML теги или LaTeX математические выражения (`$...$`)
- Для математики используй обычный текст: `2 × 3 = 6`, `√2`, `1/2`"""
        
        response_text, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [formatted_prompt, img]}], model_used)
        
        await send_long_message(placeholder_message, response_text or "Не удалось обработать изображение.")
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
    except Exception as e:
        logging.error(f"Error processing photo: {e}")
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке изображения.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
            # Fallback на новое сообщение
            await original_message.reply_text("❌ Произошла ошибка при обработке изображения.")

async def _handle_complex_agent_search(placeholder_message: Message, original_message: Message, search_prefix: str):
    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Используем `from_user.id`, а не `effective_user.id`
    user_id = original_message.from_user.id
    
    try:
        await placeholder_message.edit_text("🖼️ Анализирую изображение...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text("🖼️ Анализирую изображение...")
    
    vision_model = settings.RESEARCH_MODEL
    gemini_key, _, resolution = await _resolve_gemini_request(vision_model)
    if resolution:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {vision_model} (анализ фото) закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    img = Image.open(io.BytesIO(photo_data))
    
    analysis_prompt = prompts.IMAGE_ANALYSIS_PROMPT
    
    search_query, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': [analysis_prompt, img]}], vision_model)
    await db.increment_gemini_key_usage(gemini_key['key_hash'], vision_model)

    if not search_query:
        try:
            await placeholder_message.edit_text("Не удалось проанализировать изображение для поиска.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    chat_state = await db.get_user_chat(user_id)
    # Получаем оригинальное сообщение пользователя для локализации
    original_user_message = original_message.caption or "Опиши это изображение."
    
    if search_prefix == '?':
        await _handle_qna_search(placeholder_message, original_user_message, chat_state, search_query)
    else:
        await _handle_research_agent(placeholder_message, user_id, original_user_message, chat_state, search_query=search_query)

async def _handle_deep_dive_continuation(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState):
    """Обрабатывает продолжение deep dive разговора с сохранением контекста"""
    
    # Валидация состояния deep dive
    if not chat_state.is_deep_dive or not chat_state.deep_dive_thread_id:
        logging.warning(f"Invalid deep dive state for user {user_id} in continuation")
        try:
            await placeholder_message.edit_text("⚠️ Состояние deep dive было потеряно. Начните новый запрос с '??'")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    try:
        await placeholder_message.edit_text("🧠 Продолжаю исследование с учетом предыдущего контекста...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text("🧠 Продолжаю исследование с учетом предыдущего контекста...")
    
    # Получаем user_id и chat_id для логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    
    # Добавляем новый вопрос пользователя в историю
    chat_state.history.append({'role': 'user', 'parts': [user_message]})
    
    # Используем ту же модель, что и для предыдущего deep dive
    model_for_continuation = settings.RESEARCH_MODEL
    gemini_key, model_used, _ = await _resolve_gemini_request(model_for_continuation)
    if not gemini_key:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {model_for_continuation} закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    try:
        # Отправляем запрос с сохраненной историей и контекстом
        response_text, new_token_count = await services.get_gemini_response(
            gemini_key['api_key'], 
            chat_state.history, 
            model_used, 
            system_instruction=chat_state.system_prompt
        )
    except Exception as gemini_error:
        logging.error(f"Error in Gemini deep dive continuation: {gemini_error}")
        chat_state.history.pop()  # Убираем добавленный вопрос
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при продолжении исследования. Попробуйте позже.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    if response_text:
        # Отправляем ответ с кнопками deep dive
        await send_long_message(placeholder_message, response_text, is_deep_dive=True)
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        
        # Добавляем ответ модели в историю
        chat_state.history.append({'role': 'model', 'parts': [response_text]})
        chat_state.token_count = new_token_count
        chat_state.is_deep_dive = True  # Поддерживаем флаг deep dive
        
        # Обновляем ID потока deep dive для текущего ответа
        if placeholder_message.message_id:
            chat_state.deep_dive_thread_id = placeholder_message.message_id
        # Если message_id недоступен, сохраняем существующий
        
        await db.update_user_chat(user_id, chat_state)
    else:
        chat_state.history.pop()  # Убираем вопрос, если ответ пустой
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text("Получен пустой ответ от API при продолжении исследования.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")

async def process_long_request(placeholder_message: Message, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            
            # Сохраняем оригинальное сообщение в контексте
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data['original_message'] = update.message
            
            # Не удаляем placeholder сообщение, а редактируем его
            try:
                await placeholder_message.edit_text(
                    "Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
                # Если не можем отредактировать, отправляем новое сообщение
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return
        
        if is_photo:
            await _handle_photo(placeholder_message, update.message, chat_state)
            return

        # Проверяем, есть ли у пользователя документы для вопросов
        from ..document_processor import get_user_documents
        user_documents = await get_user_documents(update.effective_user.id)
        
        # Проверяем, является ли это продолжением deep dive
        if chat_state.is_deep_dive and not text.startswith('?') and not text.startswith('??'):
            # Валидация состояния deep dive перед продолжением
            if not chat_state.deep_dive_thread_id:
                logging.warning(f"Deep dive state corrupted for user {update.effective_user.id}, resetting")
                chat_state.is_deep_dive = False
                chat_state.deep_dive_thread_id = None
                await db.update_user_chat(update.effective_user.id, chat_state)
                await placeholder_message.edit_text("⚠️ Состояние deep dive было повреждено. Начните новый запрос с '??'")
                return
            
            # Проверка на бесконечные циклы - если сообщение слишком короткое, это может быть ошибка
            if len(text.strip()) < 3:
                logging.warning(f"Very short message in deep dive from user {update.effective_user.id}: '{text}'")
                await placeholder_message.edit_text("⚠️ Сообщение слишком короткое для продолжения deep dive. Напишите более подробный вопрос.")
                return
            
            # Логируем продолжение deep dive для отладки
            logging.info(f"Continuing deep dive for user {update.effective_user.id} with thread_id {chat_state.deep_dive_thread_id}")
            
            # Это продолжение deep dive - используем существующий контекст
            await _handle_deep_dive_continuation(placeholder_message, update.effective_user.id, text, chat_state)
            return
        
        if text.startswith('??'):
            # Новый deep dive запрос - сбрасываем флаг и начинаем заново
            chat_state.is_deep_dive = False
            chat_state.deep_dive_thread_id = None  # Сбрасываем ID потока
            await db.update_user_chat(update.effective_user.id, chat_state)
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