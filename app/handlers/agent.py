import logging
import json
import io
from PIL import Image
from typing import Optional, List
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.config import settings
from app import database as db
from app import services
from app.utils.messaging import send_long_message
from app import state
from app import prompts
from app.metrics import metrics_collector
from app.utils.api_logger import api_logger
from app.utils.formatting import escape_format_chars

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

    # Экранируем фигурные скобки в данных для предотвращения ошибок форматирования
    safe_user_message = escape_format_chars(user_message)
    safe_tavily_answer = escape_format_chars(tavily_answer)
    
    localization_prompt = prompts.QNA_LOCALIZATION_PROMPT.format(
        user_message=safe_user_message, tavily_answer=safe_tavily_answer
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

async def _handle_research_agent(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState, model_override: Optional[str] = None, search_query: str = None):
    # Если передан search_query, используем его для поиска, а user_message для локализации
    actual_search_query = search_query if search_query else user_message
    
    await metrics_collector.record_search_query()
    
    try:
        await placeholder_message.edit_text("🔎 Ищу источники...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text("🔎 Ищу источники...")
    
    # Получаем user_id и chat_id для логирования
    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    
    try:
        search_result = await services.tavily_search_agent(
            actual_search_query, 
            search_type='search',
            user_id=user_id,
            chat_id=chat_id
        )
    except Exception as search_error:
        logging.error(f"Error in Tavily search: {search_error}")
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при поиске. Попробуйте позже.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    if search_result.get("error"):
        try:
            await placeholder_message.edit_text(search_result["error"])
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    search_results = search_result.get('results', [])
    if not search_results:
        try:
            await placeholder_message.edit_text("Не удалось найти релевантные источники для исследования.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

            # Проверяем, что search_results содержит валидные данные
        valid_results = []
        if search_results:
            for result in search_results:
                if isinstance(result, dict) and result.get('url') and result.get('title'):
                    valid_results.append(result)
                else:
                    logging.warning(f"Skipping invalid search result: {result}")
        
        if not valid_results:
            try:
                await placeholder_message.edit_text("Не удалось найти валидные источники для исследования.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return
        
        search_results = valid_results  # Используем только валидные результаты

    try:
        count = len(search_results) if search_results else 0
        await placeholder_message.edit_text(f"✅ Найдено {count} источников. Выбираю лучшие...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
    
    gemini_key, model_used, _ = await _resolve_gemini_request(settings.URL_SELECTION_MODEL)
    if not gemini_key:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {settings.URL_SELECTION_MODEL} (выбор URL) закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        # Безопасная сериализация search_results
        safe_search_results = []
        if search_results:
            for result in search_results:
                safe_result = {
                    'title': str(result.get('title', '')),
                    'url': str(result.get('url', '')),
                    'content': str(result.get('content', '')),
                    'score': float(result.get('score', 0.0)) if result.get('score') is not None else 0.0
                }
                safe_search_results.append(safe_result)
        
        # Экранируем фигурные скобки в user_message для предотвращения ошибок форматирования
        from app.utils.formatting import escape_format_chars
        
        safe_user_message = escape_format_chars(user_message)
        
        selection_prompt = prompts.URL_SELECTION_PROMPT.format(
            user_message=safe_user_message,
            search_results_json=json.dumps(safe_search_results, indent=2, ensure_ascii=False)
        )
        
        # Создаем parts для Gemini API: промпт
        parts = [selection_prompt] if selection_prompt else []
        selected_urls_str, _ = await services.get_gemini_response(
            gemini_key['api_key'], 
            [{'role': 'user', 'parts': parts}], 
            model_used,
            user_id=user_id,
            chat_id=chat_id
        )
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
    except Exception as gemini_error:
        logging.error(f"Error in Gemini URL selection: {gemini_error}")
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при выборе источников. Попробуйте позже.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    selected_urls = [url.strip() for url in selected_urls_str.split(',') if url.strip().startswith('http')]

    if not selected_urls:
        try:
            await placeholder_message.edit_text("Не удалось выбрать подходящие источники для глубокого анализа. Попробуйте переформулировать запрос.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        count = len(selected_urls) if selected_urls else 0
        await placeholder_message.edit_text(f"✅ Выбрано {count} источников. Собираю контент...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
    
    final_context_list = []
    if selected_urls and search_results:
        for url in selected_urls:
            for res in search_results:
                if res.get('url') == url:
                    # Возвращаем старый формат для совместимости с Gemini API
                    # но с улучшенной структурой для AI
                    source_info = f"Источник: {res.get('url')}\nСодержание:\n{res.get('content')}"
                    final_context_list.append(source_info)
    
    full_context = "\n\n---\n\n".join(final_context_list) if final_context_list else ""

    if not full_context:
        try:
            await placeholder_message.edit_text("Не удалось собрать контент с выбранных страниц.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        count = len(full_context) if full_context else 0
        await placeholder_message.edit_text(f"🧠 Синтезирую ответ на основе {count} символов...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
    
    model_for_synthesis = model_override or settings.RESEARCH_MODEL
    gemini_key, model_used, _ = await _resolve_gemini_request(model_for_synthesis)
    if not gemini_key:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {model_for_synthesis} закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    # Экранируем фигурные скобки в данных для предотвращения ошибок форматирования
    def escape_format_chars(text: str) -> str:
        """Экранирует фигурные скобки { и } для безопасного форматирования строк"""
        if not text:
            return text
        return text.replace('{', '{{').replace('}', '}}')
    
    # Применяем экранирование к данным перед форматированием промпта
    safe_full_context = escape_format_chars(full_context)
    safe_user_message = escape_format_chars(user_message)
        
    augmented_prompt = prompts.SYNTHESIS_PROMPT.format(
        full_context=safe_full_context, user_message=safe_user_message
    )
    # Подготавливаем контекст с учётом лимитов токенов
    from app import prompts
    
    # Извлекаем суммаризацию из истории, если есть
    summary = None
    if (chat_state.history and 
        isinstance(chat_state.history, list) and 
        len(chat_state.history) > 0):
        # Проверяем, есть ли суммаризация в первом сообщении
        first_msg = chat_state.history[0]
        if (isinstance(first_msg, dict) and 
            'role' in first_msg and 
            'parts' in first_msg and 
            len(first_msg['parts']) > 0 and
            isinstance(first_msg['parts'][0], str) and
            '[Суммаризация предыдущего контекста]' in first_msg['parts'][0]):
            summary = first_msg['parts'][0]
            # Убираем суммаризацию из истории для обработки
            chat_state.history = chat_state.history[1:]
    
    # Добавляем augmented_prompt в историю
    chat_state.history.append({'role': 'user', 'parts': [augmented_prompt]})
    
    # Подготавливаем контекст с лимитами
    prepared_history, new_summary = prompts.prepare_context_with_limits(
        chat_state.history, 
        "", 
        summary
    )
    
    # Строим финальный контекст
    final_context = prompts.build_context_with_summary(
        prepared_history, 
        new_summary, 
        ""
    )
    
    # Обновляем историю в chat_state
    chat_state.history = final_context
    
    try:
        # Проверяем, что history не пустой
        if not chat_state.history or len(chat_state.history) == 0:
            try:
                await placeholder_message.edit_text("❌ История чата пуста. Невозможно обработать вопрос.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return
        
        response_text, new_token_count = await services.get_gemini_response(gemini_key['api_key'], chat_state.history, model_used, system_instruction=prompts.compose_system_instruction(chat_state.system_prompt))
    except Exception as gemini_error:
        logging.error(f"Error in Gemini synthesis: {gemini_error}")
        chat_state.history.pop()  # Убираем добавленный промпт
        await db.update_user_chat(user_id, chat_state)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при синтезе ответа. Попробуйте позже.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return
    
    if response_text and response_text.strip():
        # Проверяем, что response_text не None и не пустой
        await send_long_message(placeholder_message, response_text, is_deep_dive=True)
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        chat_state.history.append({'role': 'model', 'parts': [response_text]})
        chat_state.token_count = new_token_count
        chat_state.is_deep_dive = True
        
        # Генерируем уникальный thread_id для deep dive сессии
        import uuid
        # Безопасная проверка атрибута deep_dive_thread_id
        if not hasattr(chat_state, 'deep_dive_thread_id') or not chat_state.deep_dive_thread_id:
            chat_state.deep_dive_thread_id = str(uuid.uuid4())
            logging.info(f"Generated deep dive thread_id {chat_state.deep_dive_thread_id} for user {user_id}")
        
        await db.update_user_chat(user_id, chat_state)
        logging.info(f"Deep dive mode activated for user {user_id} with thread_id {chat_state.deep_dive_thread_id}")
    else:
        chat_state.history.pop()
        await db.update_user_chat(user_id, chat_state)
        logging.warning(f"Empty response from Gemini API for deep dive synthesis by user {user_id}")
        try:
            await placeholder_message.edit_text("Получен пустой ответ от API.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")

async def _handle_document_question(placeholder_message: Message, user_id: int, user_message: str, chat_state: db.ChatState):
    """Обрабатывает вопросы по загруженным документам"""
    try:
        # Получаем последний документ пользователя
        from app.document_processor import get_user_documents, get_document_content
        
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
        original_length = len(document_content) if document_content else 0
        if document_content and len(document_content) > max_context_length:
            document_content = document_content[:max_context_length] + "\n\n[Документ обрезан для экономии токенов]"
            logging.info(f"Document content truncated from {original_length} to {len(document_content)} characters")
        
        # Безопасная обработка document_content
        try:
            safe_document_content = str(document_content)
        except Exception as e:
            logging.error(f"Failed to convert document content to string: {e}")
            try:
                await placeholder_message.edit_text("❌ Ошибка обработки содержимого документа.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return
        
        content_length = len(safe_document_content) if safe_document_content else 0
        logging.info(f"Processing document question for user {user_id}, document: {latest_document['filename']}, content length: {content_length}")
        
        # Создаем промпт для вопроса по документу
        document_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу документов для Telegram-бота. Твоя задача — отвечать на вопросы пользователя по содержимому документа, используя правильное форматирование и предоставляя точную, полезную информацию.

# КОНТЕКСТ
**Содержимое документа:**
{safe_document_content}

**Вопрос пользователя:** {user_message}

# ПОШАГОВЫЙ ПРОЦЕСС
1. **Внимательно прочитай содержимое документа**
2. **Найди информацию, относящуюся к вопросу**
3. **Структурируй ответ логично**
4. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Технический вопрос
**Вопрос:** "Какие технологии упоминаются в документе?"
**Правильный ответ:**
*Технологии, упомянутые в документе:*
- Docker — для контейнеризации
- Python — основной язык программирования
- PostgreSQL — база данных
- Redis — кэш-система

## Пример 2: Поиск конкретной информации
**Вопрос:** "Какая версия Python используется?"
**Правильный ответ:**
*Версия Python:*
Согласно документу, используется Python версии `3.9` или выше.

_Дополнительные требования:_
- Поддержка async/await синтаксиса
- Совместимость с последними библиотеками

## Пример 3: Объяснение концепции
**Вопрос:** "Объясни архитектуру системы"
**Правильный ответ:**
*Архитектура системы:*
Документ описывает микросервисную архитектуру с следующими компонентами:

_Основные сервисы:_
- API Gateway — точка входа
- User Service — управление пользователями
- Database Service — работа с данными

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых терминов и заголовков
- `_курсив_` для вторичного акцента и определений
- `` `код` `` для технических терминов, команд и кода
- `[текст ссылки](URL)` для ссылок (если есть в документе)
- `- ` для списков

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, `<strong>`, `<em>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`
- Неэкранированные спецсимволы

# ФОРМАТИРОВАНИЕ МАТЕМАТИЧЕСКИХ ВЫРАЖЕНИЙ
## ✅ ПРАВИЛЬНО
- `2 × 3 = 6` (НЕ `$2 × 3 = 6$`)
- `√2` (НЕ `$√2$`)
- `1/2` (НЕ `$\\frac{1}{2}$`)
- `2^3 = 8` (НЕ `$2^3 = 8$`)
- `a + b = c` (НЕ `a+b=c`)
- `x = y / z` (НЕ `x=y/z`)

## ❌ НЕПРАВИЛЬНО
- `$1 × 1 = 1$` - LaTeX синтаксис
- `$$√2$$` - LaTeX синтаксис
- `a+b` - без пробелов
- `x=y` - без пробелов

# СТРУКТУРИРОВАНИЕ ОТВЕТОВ
## Для технических вопросов:
1. *Краткий ответ* - основная информация
2. _Детали_ - дополнительные сведения
3. - Список ключевых элементов
4. [Ссылки на ресурсы](URL) - если есть в документе

## Для поиска информации:
1. *Найденная информация* - что обнаружено
2. _Контекст_ - где и как это упоминается
3. - Дополнительные детали

## Для объяснения концепций:
1. *Определение* - основное понятие
2. _Принципы работы_ - как это функционирует
3. - Практические применения

# ВАЖНЫЕ ПРАВИЛА
- Отвечай ТОЛЬКО на основе содержимого документа
- Если информации недостаточно, честно скажи об этом
- Не используй предварительные знания
- Структурируй ответ согласно примерам выше
- Применяй правильное форматирование

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой ответа убедись, что:
- [ ] Ответ основан на содержимом документа
- [ ] Информация структурирована согласно примерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Математические выражения отформатированы правильно
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Все спецсимволы правильно экранированы

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
        
        # Создаем parts для Gemini API: промпт
        parts = [document_prompt] if document_prompt else []
        response_text, _ = await services.get_gemini_response(
            gemini_key['api_key'], 
            [{'role': 'user', 'parts': parts}], 
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

    # Подготавливаем контекст с учётом лимитов токенов
    from app import prompts
    
    # Извлекаем суммаризацию из истории, если есть
    summary = None
    if (chat_state.history and 
        isinstance(chat_state.history, list) and 
        len(chat_state.history) > 0):
        # Проверяем, есть ли суммаризация в первом сообщении
        first_msg = chat_state.history[0]
        if (isinstance(first_msg, dict) and 
            'role' in first_msg and 
            'parts' in first_msg and 
            len(first_msg['parts']) > 0 and
            isinstance(first_msg['parts'][0], str) and
            '[Суммаризация предыдущего контекста]' in first_msg['parts'][0]):
            summary = first_msg['parts'][0]
            # Убираем суммаризацию из истории для обработки
            chat_state.history = chat_state.history[1:]
    
    # Подготавливаем контекст с лимитами
    prepared_history, new_summary = prompts.prepare_context_with_limits(
        chat_state.history, 
        user_message, 
        summary
    )
    
    # Строим финальный контекст
    final_context = prompts.build_context_with_summary(
        prepared_history, 
        new_summary, 
        user_message
    )
    
    # Обновляем историю в chat_state
    chat_state.history = final_context
    
    try:
        await placeholder_message.edit_text(f"🧠 Модель {model_used} думает...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        # Если не можем отредактировать, отправляем новое сообщение
        placeholder_message = await placeholder_message.reply_text(f"🧠 Модель {model_used} думает...")
    
    # Используем системную инструкцию пользователя или инструкцию по умолчанию
    system_instruction = prompts.compose_system_instruction(chat_state.system_prompt)
    response_text, new_token_count = await services.get_gemini_response(gemini_key['api_key'], chat_state.history, model_used, system_instruction=system_instruction)
    
    if response_text:
        # Постоянные кнопки под ответом
        buttons = [[InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")]]
        if chat_state.is_deep_dive:
            buttons.append([InlineKeyboardButton("✨ Начать новую тему", callback_data="deepdive:new_topic")])
        reply_markup = InlineKeyboardMarkup(buttons)

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
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу изображений для Telegram-бота. Твоя задача — описать изображение, используя правильное форматирование и предоставляя детальную, полезную информацию.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ПОШАГОВЫЙ АНАЛИЗ
1. **Внимательно изучи изображение**
2. **Определи основные объекты и детали**
3. **Структурируй описание логично**
4. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Пейзаж
**Изображение:** Горный пейзаж с заснеженными вершинами
**Правильное описание:**
*Горный пейзаж* с заснеженными вершинами на фоне голубого неба.

_Детали:_
- Снежные пики отражают солнечный свет
- Внизу виднеется зеленый лес
- Облака создают драматическую атмосферу

## Пример 2: Портрет
**Изображение:** Человек в деловом костюме
**Правильное описание:**
*Человек* в деловом костюме с уверенным выражением лица.

_Характеристики:_
- Темный костюм с галстуком
- Профессиональная поза
- Фон размыт для акцента на лице

## Пример 3: Технический объект
**Изображение:** Современный автомобиль
**Правильное описание:**
*Современный автомобиль* с обтекаемым дизайном и спортивными линиями.

_Особенности:_
- Аэродинамическая форма кузова
- LED фары и стоп-сигналы
- Спортивные колесные диски

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых объектов и характеристик
- `_курсив_` для вторичных деталей и описаний
- `` `код` `` для технических терминов
- `[текст ссылки](URL)` для ссылок (если применимо)
- `- ` для списков характеристик

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`

# СТРУКТУРА ОПИСАНИЯ
1. **Основной объект** - что изображено
2. **Ключевые характеристики** - цвет, размер, стиль
3. **Контекст и окружение** - где, когда, в какой обстановке
4. **Детали и особенности** - уникальные элементы
5. **Общее впечатление** - настроение, атмосфера

# ВАЖНЫЕ ПРАВИЛА
- Будь конкретным и детальным
- Используй описательные прилагательные
- Структурируй информацию по пунктам
- Применяй правильное форматирование
- Не используй технический жаргон без объяснений
- Следуй структуре примеров выше

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой описания убедись, что:
- [ ] Описание полностью описывает изображение
- [ ] Информация структурирована согласно примерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Тон описания информативный и дружелюбный

Опиши изображение, следуя указанным инструкциям и структуре примеров."""
        
        # Создаем parts для Gemini API: текст + изображение
        parts = [formatted_prompt, img] if img else [formatted_prompt]
        response_text, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': parts}], model_used)
        
        # Проверяем, что response_text не None и не пустой
        if response_text and response_text.strip():
            await send_long_message(placeholder_message, response_text)
            # Сохраняем контекст изображения в истории
            chat_state.history.append({'role': 'user', 'parts': [formatted_prompt]})
            chat_state.history.append({'role': 'model', 'parts': [response_text]})
            await db.update_user_chat(original_message.from_user.id, chat_state)
        else:
            await send_long_message(placeholder_message, "Не удалось обработать изображение.")
            logging.warning(f"Empty response from Gemini API for image processing by user {original_message.from_user.id}")
        
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
    
    # Создаем parts для Gemini API: промпт + изображение
    parts = [analysis_prompt, img] if img else [analysis_prompt]
    search_query, _ = await services.get_gemini_response(gemini_key['api_key'], [{'role': 'user', 'parts': parts}], vision_model)
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
        from app.document_processor import get_user_documents
        user_documents = await get_user_documents(update.effective_user.id)
        
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
            from app.errors import user_friendly_error, build_retry_and_roles_keyboard
            friendly = user_friendly_error(e)
            await placeholder_message.edit_text(friendly, reply_markup=build_retry_and_roles_keyboard())
        except Exception as inner_e:
            logging.error(f"Could not edit placeholder message: {inner_e}")

async def process_media_group_request(placeholder_message: Message, update: Update, context: ContextTypes.DEFAULT_TYPE, messages: List[Message], caption: str):
    """Обрабатывает группу изображений как единое целое"""
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)
    
    count = len(messages) if messages else 0
    logging.info(f"🔄 Обрабатываю группу из {count} изображений для пользователя {user_id}")
    
    # Проверяем, есть ли поисковый префикс в caption
    search_prefix = None
    if caption:
        if caption.startswith('??'):
            search_prefix = '??'
        elif caption.startswith('?'):
            search_prefix = '?'
    
    # Если есть поисковый префикс, используем сложный поиск
    if search_prefix:
        await _handle_complex_media_group_search(placeholder_message, messages, caption, search_prefix, chat_state)
    else:
        # Обычная обработка группы изображений
        await _handle_media_group_photos(placeholder_message, messages, caption, chat_state)

async def _handle_media_group_photos(placeholder_message: Message, messages: List[Message], caption: str, chat_state: db.ChatState):
    """Обрабатывает группу изображений для обычного описания"""
    gemini_key, model_used, resolution = await _resolve_gemini_request(chat_state.model)
    if resolution:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {chat_state.model} для обработки фото закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        # Загружаем все изображения из группы
        images = []
        for i, message in enumerate(messages):
            try:
                photo_file = await message.photo[-1].get_file()
                photo_data = await photo_file.download_as_bytearray()
                img = Image.open(io.BytesIO(photo_data))
                images.append(img)
                count = len(messages) if messages else 0
                logging.info(f"📸 Загружено изображение {i+1}/{count}")
            except Exception as e:
                logging.error(f"Error loading image {i+1}: {e}")
                continue
        
        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения из группы.")
            return
        
        # Формируем промпт для группы изображений
        count = len(images) if images else 0
        prompt = caption or f"Опиши эти {count} изображения."
        
        # Добавляем инструкции по форматированию
        formatted_prompt = f"""# РОЛЬ И ЗАДАЧА
Ты — эксперт по анализу групп изображений для Telegram-бота. Твоя задача — описать группу изображений, используя правильное форматирование и предоставляя детальную, полезную информацию.

# КОНТЕКСТ
**Запрос пользователя:** {prompt}

# ПОШАГОВЫЙ АНАЛИЗ
1. **Внимательно изучи каждое изображение**
2. **Определи основные объекты и детали**
3. **Проанализируй связи между изображениями**
4. **Структурируй описание логично**
5. **Примени правильное MarkdownV2 форматирование**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Последовательность событий
**Группа:** 3 изображения процесса приготовления блюда
**Правильное описание:**
*Группа изображений показывает процесс приготовления блюда:*

_Изображение 1:_ Подготовка ингредиентов на кухонном столе
_Изображение 2:_ Процесс готовки на плите
_Изображение 3:_ Готовое блюдо на тарелке

## Пример 2: Разные аспекты темы
**Группа:** 4 изображения разных типов автомобилей
**Правильное описание:**
*Коллекция различных типов автомобилей:*

_Изображение 1:_ *Спортивный автомобиль* с обтекаемым дизайном
_Изображение 2:_ *Внедорожник* с высоким клиренсом
_Изображение 3:_ *Семейный седан* с практичным салоном
_Изображение 4:_ *Электромобиль* с современным дизайном

## Пример 3: Сравнение или контраст
**Группа:** 2 изображения старого и нового здания
**Правильное описание:**
*Сравнение архитектурных стилей:*

_Изображение 1:_ *Классическое здание* с традиционными элементами
_Изображение 2:_ *Современное здание* с инновационным дизайном

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых объектов и характеристик
- `_курсив_` для вторичных деталей и описаний
- `` `код` `` для технических терминов
- `[текст ссылки](URL)` для ссылок (если применимо)
- `- ` для списков характеристик

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`

# СТРУКТУРА ОПИСАНИЯ ГРУППЫ
1. **Общий контекст** - что представляет группа изображений
2. **Индивидуальные описания** - каждое изображение отдельно
3. **Связи и отношения** - как изображения связаны между собой
4. **Общие темы** - что объединяет все изображения
5. **Общее впечатление** - итоговое восприятие группы

# ВАЖНЫЕ ПРАВИЛА
- Пронумеруй изображения для ясности
- Опиши каждое изображение отдельно
- Выдели связи между изображениями
- Будь конкретным и детальным
- Используй описательные прилагательные
- Структурируй информацию по пунктам
- Применяй правильное форматирование
- Не используй технический жаргон без объяснений
- Следуй структуре примеров выше

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой описания убедись, что:
- [ ] Описание полностью описывает группу изображений
- [ ] Каждое изображение описано отдельно
- [ ] Выделены связи между изображениями
- [ ] Информация структурирована согласно примерам
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Тон описания информативный и дружелюбный

Опиши группу изображений, следуя указанным инструкциям и структуре примеров."""
        
        # Создаем parts для Gemini API: текст + все изображения
        parts = [formatted_prompt] + (images or [])
        
        # Получаем user_id и chat_id для логирования
        user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None
        
        response_text, _ = await services.get_gemini_response(
            gemini_key['api_key'], 
            [{'role': 'user', 'parts': parts}], 
            model_used,
            user_id=user_id,
            chat_id=chat_id
        )
        
        await send_long_message(placeholder_message, response_text or "Не удалось обработать группу изображений.")
        await db.increment_gemini_key_usage(gemini_key['key_hash'], model_used)
        
        count = len(images) if images else 0
        logging.info(f"✅ Группа из {count} изображений обработана успешно")
        
    except Exception as e:
        logging.error(f"Error processing media group photos: {e}")
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке группы изображений.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")

async def _handle_complex_media_group_search(placeholder_message: Message, messages: List[Message], caption: str, search_prefix: str, chat_state: db.ChatState):
    """Обрабатывает группу изображений для сложного поиска"""
    user_id = placeholder_message.from_user.id
    
    try:
        await placeholder_message.edit_text("🖼️ Анализирую группу изображений...")
    except Exception as edit_error:
        logging.error(f"Could not edit placeholder message: {edit_error}")
        placeholder_message = await placeholder_message.reply_text("🖼️ Анализирую группу изображений...")
    
    vision_model = settings.RESEARCH_MODEL
    gemini_key, _, resolution = await _resolve_gemini_request(vision_model)
    if resolution:
        try:
            await placeholder_message.edit_text(f"🚫 Ключи для модели {vision_model} (анализ фото) закончились.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
        return

    try:
        # Загружаем все изображения из группы
        images = []
        for i, message in enumerate(messages):
            try:
                photo_file = await message.photo[-1].get_file()
                photo_data = await photo_file.download_as_bytearray()
                img = Image.open(io.BytesIO(photo_data))
                images.append(img)
                count = len(messages) if messages else 0
                logging.info(f"📸 Загружено изображение {i+1}/{count} для анализа")
            except Exception as e:
                logging.error(f"Error loading image {i+1}: {e}")
                continue
        
        if not images:
            await placeholder_message.edit_text("❌ Не удалось загрузить ни одного изображения для анализа.")
            return
        
        # Анализируем группу изображений для поиска
        analysis_prompt = f"""{prompts.IMAGE_ANALYSIS_PROMPT}

# ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ДЛЯ ГРУППЫ ИЗОБРАЖЕНИЙ
## Анализ группы
- Проанализируй все изображения как единый контекст
- Выдели общие темы, объекты или концепции
- Учти взаимосвязи между изображениями
- Создай поисковый запрос, который охватывает весь контекст группы

## Специальные случаи
- Если изображения показывают последовательность или процесс, отрази это в запросе
- Если изображения демонстрируют разные аспекты одной темы, объедини их
- Если изображения показывают сравнение или контраст, укажи это

# FEW-SHOT ПРИМЕРЫ ДЛЯ ГРУПП
## Пример 1: Последовательность событий
**Группа:** 3 изображения процесса приготовления блюда
**Правильный запрос:** `cooking process step by step recipe preparation`

## Пример 2: Разные аспекты темы
**Группа:** 4 изображения разных типов автомобилей
**Правильный запрос:** `car types comparison sedan SUV sports luxury vehicles`

## Пример 3: Контраст или сравнение
**Группа:** 2 изображения старого и нового здания
**Правильный запрос:** `architecture evolution old vs new building comparison`

# ФОРМАТ ВЫВОДА
Верни ТОЛЬКО поисковый запрос без:
- Кавычек
- Двоеточий
- Объяснений
- Вводных фраз
- Дополнительного форматирования

**Пример правильного вывода:**
```
cooking process step by step recipe preparation
```"""
        
        # Создаем parts для анализа: промпт + все изображения
        parts = [analysis_prompt] + (images or [])
        
        # Получаем user_id и chat_id для логирования
        user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
        chat_id = placeholder_message.chat.id if placeholder_message.chat else None
        
        search_query, _ = await services.get_gemini_response(
            gemini_key['api_key'], 
            [{'role': 'user', 'parts': parts}], 
            vision_model,
            user_id=user_id,
            chat_id=chat_id
        )
        await db.increment_gemini_key_usage(gemini_key['key_hash'], vision_model)

        if not search_query:
            try:
                await placeholder_message.edit_text("Не удалось проанализировать группу изображений для поиска.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            return

        # Получаем оригинальное сообщение пользователя для локализации
        count = len(images) if images else 0
        original_user_message = caption or f"Опиши эти {count} изображения."
        
        if search_prefix == '?':
            await _handle_qna_search(placeholder_message, original_user_message, chat_state, search_query)
        else:
            await _handle_research_agent(placeholder_message, user_id, original_user_message, chat_state, search_query=search_query)
        
        count = len(images) if images else 0
        logging.info(f"✅ Группа из {count} изображений проанализирована для поиска")
        
    except Exception as e:
        logging.error(f"Error processing complex media group search: {e}")
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при анализе группы изображений.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")