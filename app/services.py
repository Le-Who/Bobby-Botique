import logging
import httpx
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import Dict, Any, List
import asyncio

from .config import settings
from . import database
from .metrics import metrics_collector
from .cache import get_cached_search_result, get_cached_search_result_with_metadata, cache_search_result

http_client = httpx.AsyncClient(timeout=30.0)

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None):
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
                    logging.debug(f"Gemini API response candidate: finish_reason={getattr(candidate, 'finish_reason', 'unknown')}, parts_count={len(getattr(candidate, 'content', {}).get('parts', [])) if hasattr(candidate, 'content') else 0}")
                    
                    if hasattr(candidate, 'finish_reason'):
                        if candidate.finish_reason == 1:  # SAFETY
                            logging.warning(f"Gemini API response blocked due to safety concerns. Prompt length: {len(str(history[-1]['parts']))}, model: {model_name}")
                            await metrics_collector.record_error("gemini_safety", "Response blocked by safety filters")
                            return "🚫 Ответ заблокирован системой безопасности Gemini. Попробуйте переформулировать запрос.", None
                        elif candidate.finish_reason == 2:  # RECITATION
                            logging.warning(f"Gemini API response blocked due to recitation concerns. Prompt length: {len(str(history[-1]['parts']))}, model: {model_name}")
                            await metrics_collector.record_error("gemini_recitation", "Response blocked by recitation filters")
                            return "🚫 Ответ заблокирован из-за проблем с повторением. Попробуйте переформулировать запрос.", None
                        elif candidate.finish_reason == 3:  # OTHER
                            logging.warning(f"Gemini API response blocked for other reasons. Prompt length: {len(str(history[-1]['parts']))}, model: {model_name}")
                            await metrics_collector.record_error("gemini_other", f"Response blocked, finish_reason: {candidate.finish_reason}")
                            return "🚫 Ответ заблокирован по техническим причинам. Попробуйте позже.", None
                        else:
                            logging.info(f"Gemini API response finish_reason: {candidate.finish_reason} (normal completion)")
                else:
                    logging.warning("Gemini API response has no candidates")
                    await metrics_collector.record_error("gemini_no_candidates", "Response has no candidates")
                    return "🚫 Получен некорректный ответ от API. Попробуйте позже.", None
                
                # Проверяем, что ответ содержит валидные части
                try:
                    response_text = response.text
                    if not response_text:
                        logging.warning("Gemini API returned empty response")
                        await metrics_collector.record_error("gemini_empty", "Empty response from API")
                        return "🚫 Получен пустой ответ от API. Попробуйте переформулировать запрос.", None
                except Exception as text_error:
                    logging.error(f"Error accessing response.text: {text_error}. Response object: {type(response)}, has candidates: {hasattr(response, 'candidates')}, candidates count: {len(getattr(response, 'candidates', []))}")
                    await metrics_collector.record_error("gemini_text_access", str(text_error))
                    return "🚫 Ошибка при обработке ответа API. Попробуйте позже.", None
                
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
            return f"Произошла ошибка API: {error_msg}", None
    
    # Этот код не должен выполниться, но на всякий случай
    return "🚫 Произошла непредвиденная ошибка API после всех попыток.", None

async def tavily_search_agent(query: str, search_type: str = "search"):
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

    if search_type == "qna":
        payload["search_depth"] = "basic"
        cost = settings.TAVILY_QNA_SEARCH_COST
        logging.debug(f"QNA search payload: {payload}")
    else:
        payload["search_depth"] = "advanced"
        payload["max_results"] = 7
        cost = settings.TAVILY_ADVANCED_SEARCH_COST
        logging.debug(f"Advanced search payload: {payload}")

    try:
        response = await http_client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        
        data = response.json()
        logging.info(f"Tavily API response received for query: {query[:50]}... (status: {response.status_code})")
        await database.increment_tavily_key_usage(available_key['key_hash'], cost)
        
        result = {}
        if search_type == "qna":
            result = {"type": "answer", "content": data.get("answer", "")}
            logging.info(f"QNA result extracted: content length {len(data.get('answer', ''))}")
        else:
            result = {"type": "search", "results": data.get('results', [])}
            logging.info(f"Search result extracted: {len(data.get('results', []))} results")
        
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
        logging.error(f"Tavily API call failed with status {e.response.status_code}: {e.response.text}")
        await metrics_collector.record_error("tavily_http", f"Status {e.response.status_code}: {e.response.text}")
        return {"error": f"Ошибка API поиска: {e.response.status_code}. Убедитесь, что ключ API валиден."}
    except Exception as e:
        logging.error(f"Tavily API call failed: {e}")
        await metrics_collector.record_error("tavily_api", str(e))
        return {"error": f"Произошла ошибка во время вызова API поиска: {e}"}
