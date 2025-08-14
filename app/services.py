import logging
import httpx
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import Dict, Any, List
import asyncio
from contextlib import asynccontextmanager

from .config import settings
from . import database
from .metrics import metrics_collector
from .cache import get_cached_search_result, get_cached_search_result_with_metadata, cache_search_result

# Улучшенные настройки HTTP клиента
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=25.0, write=10.0)
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
    """Контекстный менеджер для HTTP клиента с правильным управлением ресурсами"""
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
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
    
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            # Записываем метрики API вызова
            await metrics_collector.record_api_call("gemini", model_name)
            
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(model_name, safety_settings=settings.SAFETY_SETTINGS, system_instruction=system_instruction)
            chat = model.start_chat(history=history[:-1])
            
            # Добавляем таймаут для API вызова
            try:
                response = await asyncio.wait_for(
                    chat.send_message_async(history[-1]['parts']),
                    timeout=60.0  # 60 секунд таймаут
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
                    
                    logging.debug(f"Gemini API response candidate: finish_reason={finish_reason}, parts_count={parts_count}")
                    logging.debug(f"Response object type: {type(response)}, candidate type: {type(candidate)}, content type: {type(content)}")
                    
                    if finish_reason is not None:
                        if finish_reason == FINISH_REASON_SAFETY:  # SAFETY
                            logging.warning(f"Gemini API response blocked due to safety concerns. Prompt length: {len(str(history[-1]['parts']))}, model: {model_name}")
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
                    await metrics_collector.record_error("gemini_timeout", "60s timeout reached")
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
            await metrics_collector.record_error("gemini_api", error_msg)
            
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
