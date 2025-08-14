import logging
import httpx
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import Dict, Any, List
import asyncio
from contextlib import asynccontextmanager

from .config import settings, get_safety_settings
from .settings_service import get_int as settings_get_int, get_bool as settings_get_bool, get_setting as settings_get
from . import database
from .metrics import metrics_collector
from .cache import get_cached_search_result, get_cached_search_result_with_metadata, cache_search_result

# Улучшенные настройки HTTP клиента
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10)

# Константы для finish_reason
FINISH_REASON_SAFETY = 1
FINISH_REASON_RECITATION = 2
FINISH_REASON_OTHER = 3

# Константы для типов поиска
SEARCH_TYPE_SEARCH = "search"
SEARCH_TYPE_QNA = "qna"

@asynccontextmanager
async def get_http_client():
    """Контекстный менеджер для HTTP клиента с динамическим таймаутом из настроек"""
    total_timeout = max(5, await settings_get_int("REQUEST_TIMEOUT_SECONDS"))
    timeout = httpx.Timeout(total=total_timeout)
    async with httpx.AsyncClient(
        timeout=timeout,
        limits=HTTP_LIMITS,
        headers={"User-Agent": "Gemaibot/2.0"}
    ) as client:
        yield client

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None) -> tuple[str, int | None]:
    """
    Получает ответ от Gemini API с поддержкой retry логики и обработки ошибок.
    
    Args:
        api_key: API ключ для Gemini
        history: История чата в формате [{'role': 'user', 'parts': [...]}]
        model_name: Название модели Gemini
        system_instruction: Системная инструкция (опционально)
    
    Returns:
        tuple: (текст_ответа, количество_токенов) или (сообщение_об_ошибке, None)
    
    Raises:
        None: Все ошибки обрабатываются внутри функции
    """
    # Валидация входных параметров
    if not api_key or not history or not model_name:
        logging.error("Invalid input parameters: api_key, history, or model_name is empty")
        return "🚫 Некорректные параметры запроса.", None
    
    if not isinstance(history, list) or len(history) < 1:
        logging.error("Invalid history format: must be non-empty list")
        return "🚫 Некорректный формат истории.", None
    
    # Детальное логирование для отладки
    logging.info(f"=== GEMINI API REQUEST DEBUG ===")
    logging.info(f"Model: {model_name}")
    logging.info(f"History length: {len(history)}")
    logging.info(f"Last message parts: {history[-1].get('parts', [])}")
    logging.info(f"System instruction: {system_instruction}")
    logging.info(f"Available safety settings: Standard={settings.SAFETY_SETTINGS}, Relaxed={settings.SAFETY_SETTINGS_RELAXED}, Disabled={settings.SAFETY_SETTINGS_DISABLED}")
    
    max_retries = max(1, await settings_get_int("MAX_RETRIES"))
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            # Записываем метрики API вызова
            try:
                await metrics_collector.record_api_call("gemini", model_name)
            except Exception as metric_error:
                logging.warning(f"Failed to record API call metric: {metric_error}")
            
            genai.configure(api_key=api_key)
            
            # Выбираем настройки безопасности в зависимости от попытки и конфигурации
            enable_safety_fallback = await settings_get_bool("ENABLE_SAFETY_FALLBACK")
            
            # Получаем текущий режим безопасности из базы данных
            current_safety_mode = await settings_get("SAFETY_MODE")
            if not current_safety_mode:
                current_safety_mode = "auto"  # Значение по умолчанию
            
            if not enable_safety_fallback:
                # Если fallback отключен, используем только указанный режим
                current_safety_settings = get_safety_settings(current_safety_mode)
                logging.info(f"Using fixed safety settings for mode '{current_safety_mode}': {current_safety_settings}")
            else:
                # Автоматическое переключение настроек безопасности
                if attempt == 0:
                    current_safety_settings = get_safety_settings(current_safety_mode)
                    logging.info(f"Using safety settings for mode '{current_safety_mode}' on attempt {attempt + 1}")
                elif attempt == 1:
                    # На второй попытке используем расслабленные настройки
                    current_safety_settings = get_safety_settings("relaxed")
                    logging.info(f"Using relaxed safety settings on attempt {attempt + 1}")
                else:
                    # На третьей попытке отключаем безопасность
                    current_safety_settings = get_safety_settings("disabled")
                    logging.info(f"Using disabled safety settings on attempt {attempt + 1}")
            
            # Проверяем, нужно ли использовать system_instruction
            enable_system_instruction_fallback = await settings_get_bool("ENABLE_SYSTEM_INSTRUCTION_FALLBACK")
            if not enable_system_instruction_fallback:
                current_system_instruction = system_instruction
                logging.info(f"System instruction fallback disabled, always using: {bool(current_system_instruction)}")
            else:
                # На первом попытке используем system_instruction, на повторных - без него
                current_system_instruction = system_instruction if attempt == 0 else None
                if current_system_instruction:
                    logging.info(f"Using system_instruction on attempt {attempt + 1}")
                else:
                    logging.info(f"No system_instruction on attempt {attempt + 1}")
            
            model = genai.GenerativeModel(model_name, safety_settings=current_safety_settings, system_instruction=current_system_instruction)
            chat = model.start_chat(history=history[:-1])
            
            # Добавляем таймаут для API вызова
            try:
                request_timeout = max(5, await settings_get_int("REQUEST_TIMEOUT_SECONDS"))
                response = await asyncio.wait_for(
                    chat.send_message_async(history[-1]['parts']),
                    timeout=float(request_timeout)
                )
                
                # Проверяем finish_reason ответа
                if hasattr(response, 'candidates') and response.candidates:
                    candidate = response.candidates[0]
                    # Правильный доступ к атрибутам candidate
                    finish_reason = getattr(candidate, 'finish_reason', None)
                    content = getattr(candidate, 'content', None)
                    parts_count = 0
                    if content and hasattr(content, 'parts'):
                        parts_count = len(content.parts)
                    
                    logging.info(f"=== GEMINI API RESPONSE DEBUG ===")
                    logging.info(f"Finish reason: {finish_reason}")
                    logging.info(f"Parts count: {parts_count}")
                    logging.info(f"Response object type: {type(response)}")
                    logging.info(f"Candidate type: {type(candidate)}")
                    logging.info(f"Content type: {type(content)}")
                    
                    if finish_reason is not None:
                        if finish_reason == FINISH_REASON_SAFETY:  # SAFETY
                            logging.error(f"🚨 SAFETY BLOCK: Gemini API response blocked due to safety concerns")
                            logging.error(f"Prompt length: {len(str(history[-1]['parts']))}")
                            logging.error(f"Model: {model_name}")
                            logging.error(f"Current safety settings: {current_safety_settings}")
                            logging.error(f"System instruction used: {current_system_instruction}")
                            logging.error(f"Last message parts: {history[-1].get('parts', [])}")
                            logging.error(f"Attempt: {attempt + 1}/{max_retries}")
                            
                            # Попробуем отправить без system_instruction
                            if system_instruction and attempt < max_retries - 1:
                                logging.warning(f"Retrying without system_instruction on attempt {attempt + 1}")
                                continue
                            
                            # Если это последняя попытка, попробуем упростить промпт
                            enable_prompt_simplification = await settings_get_bool("ENABLE_PROMPT_SIMPLIFICATION")
                            if attempt < max_retries - 1 and enable_prompt_simplification:
                                logging.warning(f"Retrying with simplified prompt on attempt {attempt + 1}")
                                # Упрощаем промпт, убирая потенциально проблемные части
                                simplified_parts = []
                                for part in history[-1]['parts']:
                                    if isinstance(part, str):
                                        # Убираем специальные символы и форматирование
                                        simplified_part = part.replace('*', '').replace('_', '').replace('`', '')
                                        simplified_parts.append(simplified_part)
                                    else:
                                        simplified_parts.append(part)
                                
                                history[-1]['parts'] = simplified_parts
                                logging.info(f"Simplified prompt: {simplified_parts}")
                                continue
                            
                            await metrics_collector.record_error("gemini_safety", "Response blocked by safety filters")
                            return "🚫 Ответ заблокирован системой безопасности Gemini. Попробуйте переформулировать запрос.", None
                        elif finish_reason == FINISH_REASON_RECITATION:  # RECITATION
                            logging.warning(f"Gemini API response blocked due to recitation concerns. Prompt length: {len(str(history[-1]['parts']))}, model: {model_name}")
                            await metrics_collector.record_error("gemini_recitation", "Response blocked by recitation filters")
                            return "🚫 Ответ заблокирован из-за проблем с повторением. Попробуйте переформулировать запрос.", None
                        elif finish_reason == FINISH_REASON_OTHER:  # OTHER
                            logging.warning(f"Gemini API response blocked for other reasons. Prompt length: {len(str(history[-1]['parts']))}, model: {model_name}")
                            await metrics_collector.record_error("gemini_other", f"Response blocked, finish_reason: {finish_reason}")
                            return "🚫 Ответ заблокирован по техническим причинам. Попробуйте позже.", None
                        else:
                            logging.info(f"Gemini API response finish_reason: {finish_reason} (normal completion)")
                else:
                    logging.warning("Gemini API response has no candidates")
                    await metrics_collector.record_error("gemini_no_candidates", "Response has no candidates")
                    return "🚫 Получен некорректный ответ от API. Попробуйте позже.", None
                
                # Проверяем, что ответ содержит валидные части
                response_text = None
                try:
                    response_text = response.text
                    if not response_text:
                        logging.warning("Gemini API returned empty response")
                        await metrics_collector.record_error("gemini_empty", "Empty response from API")
                        return "🚫 Получен пустой ответ от API. Попробуйте переформулировать запрос.", None
                except Exception as text_error:
                    logging.warning(f"response.text not available: {text_error}, trying alternative method")
                    # Пробуем альтернативный способ получения текста
                    try:
                        # Используем уже проверенные candidate и content
                        if content and hasattr(content, 'parts'):
                            # Собираем текст из частей
                            response_text = ""
                            for part in content.parts:
                                if hasattr(part, 'text'):
                                    response_text += part.text
                            
                            if response_text:
                                logging.info("Successfully extracted text using alternative method")
                            else:
                                logging.error("Alternative method returned empty text")
                                await metrics_collector.record_error("gemini_alternative_empty", "Alternative text extraction returned empty")
                                return "🚫 Не удалось получить текст ответа. Попробуйте позже.", None
                        else:
                            logging.error("No content.parts available in candidate")
                            await metrics_collector.record_error("gemini_no_content_parts", "No content.parts in candidate")
                            return "🚫 Некорректная структура ответа API. Попробуйте позже.", None
                    except Exception as alt_error:
                        logging.error(f"Alternative text extraction also failed: {alt_error}")
                        await metrics_collector.record_error("gemini_text_access", f"Both methods failed: {text_error}, {alt_error}")
                        return "🚫 Ошибка при обработке ответа API. Попробуйте позже.", None
                
                if not response_text:
                    logging.error("Failed to extract response text from both methods")
                    await metrics_collector.record_error("gemini_text_extraction_failed", "Both text extraction methods failed")
                    return "🚫 Не удалось получить текст ответа. Попробуйте позже.", None
                
                token_count = model.count_tokens(chat.history).total_tokens
                return response_text, token_count
            except asyncio.TimeoutError:
                logging.error(f"Gemini API timeout on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    logging.info(f"Retrying due to timeout...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                else:
                    await metrics_collector.record_error("gemini_timeout", "Timeout reached")
                    return "🚫 Превышен таймаут ожидания ответа от API. Попробуйте позже.", None
            
        except google_exceptions.ResourceExhausted as e:
            logging.error(f"Gemini API Quota Error: {e}")
            await metrics_collector.record_error("gemini_quota", str(e))
            return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
            
        except Exception as e:
            error_msg = str(e)
            logging.error(f"Gemini API error (attempt {attempt + 1}/{max_retries}): {error_msg}")
            
            # Если это ошибка 500, пробуем повторить
            if "500" in error_msg and "internal error" in error_msg.lower():
                logging.warning(f"Detected 500 internal error on attempt {attempt + 1}: {error_msg}")
                if attempt < max_retries - 1:
                    logging.info(f"Retrying in {retry_delay} seconds due to 500 error...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Экспоненциальная задержка
                    continue
                else:
                    logging.error("Max retries reached for 500 error")
                    await metrics_collector.record_error("gemini_500_retry_exhausted", error_msg)
                    return "🚫 Сервер Gemini временно недоступен (ошибка 500). Попробуйте позже.", None
            
            # Для других ошибок не повторяем
            logging.error(f"Non-retryable error: {error_msg}")
            try:
                await metrics_collector.record_error("gemini_api", error_msg)
            except Exception as metric_error:
                logging.warning(f"Failed to record error metric: {metric_error}")
            
            # Улучшенные сообщения об ошибках
            if "rate limit" in error_msg.lower() or "quota" in error_msg.lower():
                return "🚫 Достигнут лимит запросов к API. Попробуйте позже.", None
            elif "authentication" in error_msg.lower() or "invalid api key" in error_msg.lower():
                return "🚫 Ошибка аутентификации API. Обратитесь к администратору.", None
            elif "model not found" in error_msg.lower():
                return "🚫 Указанная модель не найдена. Обратитесь к администратору.", None
            else:
                return f"🚫 Произошла ошибка API: {error_msg[:100]}...", None
    
    # Этот код не должен выполниться, но на всякий случай
    return "🚫 Произошла непредвиденная ошибка API после всех попыток.", None

async def tavily_search_agent(query: str, search_type: str = "search") -> Dict[str, Any]:
    """
    Выполняет поиск через Tavily API с поддержкой кэширования и обработки ошибок.
    
    Args:
        query: Поисковый запрос
        search_type: Тип поиска ("search" для обычного поиска, "qna" для вопрос-ответ)
    
    Returns:
        Dict: Результат поиска с метаданными или сообщение об ошибке
        
        Успешный результат:
        {
            'data': {
                'type': 'answer' | 'search',
                'content': str,  # для QNA
                'results': list   # для обычного поиска
            },
            'from_cache': bool,
            'cache_key': str | None
        }
        
        Ошибка:
        {
            'error': str
        }
    
    Raises:
        None: Все ошибки обрабатываются внутри функции
    """
    # Валидация входных параметров
    if not query or not query.strip():
        logging.error("Empty or invalid query provided")
        return {"error": "Пустой или некорректный запрос."}
    
    if search_type not in [SEARCH_TYPE_SEARCH, SEARCH_TYPE_QNA]:
        logging.error(f"Invalid search_type: {search_type}")
        return {"error": "Некорректный тип поиска."}
    
    # Проверяем кэш перед выполнением поиска
    cached_result = await get_cached_search_result_with_metadata(query, search_type)
    if cached_result:
        logging.info(f"Cache hit for Tavily search: {query[:50]}... (type: {search_type})")
        return cached_result
    
    # Если кэш пуст, выполняем поиск
    logging.info(f"Cache miss for Tavily search: {query[:50]}... (type: {search_type}), performing fresh search")
    
    available_key = await database.get_available_tavily_key()
    if not available_key:
        logging.error(f"No available Tavily API key for search: {query[:50]}...")
        return {"error": "Поиск недоступен: все API ключи сервиса поиска достигли месячного лимита."}
    
    api_key = available_key['api_key']
    logging.info(f"Performing Tavily API call (type: {search_type}) for query: {query[:100]}")
    
    # Записываем метрики поискового запроса
    await metrics_collector.record_search_query()
    await metrics_collector.record_api_call("tavily", search_type)
    
    payload = {"api_key": api_key, "query": query}
    cost = 0

    if search_type == SEARCH_TYPE_QNA:
        payload["search_depth"] = "basic"
        cost = settings.TAVILY_QNA_SEARCH_COST
        logging.debug(f"QNA search payload: {payload}")
    else:
        payload["search_depth"] = "advanced"
        payload["max_results"] = 7
        cost = settings.TAVILY_ADVANCED_SEARCH_COST
        logging.debug(f"Advanced search payload: {payload}")

    try:
        async with get_http_client() as client:
            response = await client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
            
            data = response.json()
            logging.info(f"Tavily API response received for query: {query[:50]}... (status: {response.status_code})")
            
            # Проверяем структуру ответа
            if not isinstance(data, dict):
                logging.error(f"Invalid response format from Tavily API: {type(data)}")
                await metrics_collector.record_error("tavily_invalid_format", f"Response is not dict: {type(data)}")
                return {"error": "Некорректный формат ответа от API поиска."}
            
            await database.increment_tavily_key_usage(available_key['key_hash'], cost)
            
            result = {}
            if search_type == SEARCH_TYPE_QNA:
                answer = data.get("answer", "")
                if not answer:
                    logging.warning("Tavily API returned empty answer for QNA search")
                    await metrics_collector.record_error("tavily_empty_answer", "Empty answer from QNA search")
                    return {"error": "Не удалось получить ответ на вопрос. Попробуйте переформулировать запрос."}
                
                result = {"type": "answer", "content": answer}
                logging.info(f"QNA result extracted: content length {len(answer)}")
            else:
                results = data.get("results", [])
                if not results:
                    logging.warning("Tavily API returned empty results for search")
                    await metrics_collector.record_error("tavily_empty_results", "Empty results from search")
                    return {"error": "Не удалось найти результаты поиска. Попробуйте переформулировать запрос."}
                
                result = {"type": "search", "results": results}
                logging.info(f"Search result extracted: {len(results)} results")
            
            # Сохраняем результат в кэш
            await cache_search_result(query, search_type, result)
            logging.info(f"Result cached for query: {query[:50]}... (type: {search_type})")
            
            # Возвращаем результат с пометкой, что он не из кэша
            return {
                'data': result,
                'from_cache': False,
                'cache_key': None
            }

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        error_text = e.response.text[:200] if e.response.text else "Нет деталей"
        
        logging.error(f"Tavily API call failed with status {status_code}: {error_text}")
        await metrics_collector.record_error("tavily_http", f"Status {status_code}: {error_text}")
        
        # Улучшенные сообщения об ошибках по статус кодам
        if status_code == 401:
            return {"error": "Ошибка аутентификации API. Проверьте ключ API."}
        elif status_code == 403:
            return {"error": "Доступ к API запрещен. Проверьте права доступа."}
        elif status_code == 429:
            return {"error": "Превышен лимит запросов к API. Попробуйте позже."}
        elif status_code >= 500:
            return {"error": f"Ошибка сервера API ({status_code}). Попробуйте позже."}
        else:
            return {"error": f"Ошибка API поиска: {status_code}. {error_text}"}
            
    except httpx.RequestError as e:
        logging.error(f"Tavily API request failed: {e}")
        await metrics_collector.record_error("tavily_request", str(e))
        
        # Улучшенные сообщения об ошибках соединения
        if "timeout" in str(e).lower():
            return {"error": "Превышен таймаут соединения с API поиска. Попробуйте позже."}
        elif "connection" in str(e).lower():
            return {"error": "Ошибка соединения с API поиска. Проверьте интернет-соединение."}
        else:
            return {"error": "Ошибка соединения с API поиска. Попробуйте позже."}
            
    except Exception as e:
        logging.error(f"Tavily API call failed: {e}")
        await metrics_collector.record_error("tavily_api", str(e))
        return {"error": f"Произошла ошибка во время вызова API поиска: {str(e)[:100]}..."}
