import logging
import httpx
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import Dict, Any, List

from .config import settings
from . import database
from .metrics import metrics_collector
from .cache import get_cached_search_result, get_cached_search_result_with_metadata, cache_search_result

http_client = httpx.AsyncClient(timeout=30.0)

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None):
    try:
        # Записываем метрики API вызова
        await metrics_collector.record_api_call("gemini", model_name)
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name, safety_settings=settings.SAFETY_SETTINGS, system_instruction=system_instruction)
        chat = model.start_chat(history=history[:-1])
        response = await chat.send_message_async(history[-1]['parts'])
        token_count = model.count_tokens(chat.history).total_tokens
        return response.text, token_count
    except google_exceptions.ResourceExhausted as e:
        logging.error(f"Gemini API Quota Error: {e}")
        await metrics_collector.record_error("gemini_quota", str(e))
        return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
    except Exception as e:
        logging.error(f"Gemini API generic error: {e}")
        await metrics_collector.record_error("gemini_api", str(e))
        return f"Произошла непредвиденная ошибка API: {e}", None

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
