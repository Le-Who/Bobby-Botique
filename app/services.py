import logging
import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError
from typing import Dict, Any, List
from PIL import Image

from .config import settings
from . import database
from .metrics import metrics_collector
from .cache import get_cached_search_result, cache_search_result
from .utils.network import NetworkErrorHandler

# Используем улучшенную конфигурацию HTTP клиента
http_client = NetworkErrorHandler.create_robust_http_client()

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None):
    try:
        await metrics_collector.record_api_call("gemini", model_name)
        
        client = genai.Client(api_key=api_key)
        
        # Преобразуем историю в формат types.Content
        contents = []
        for item in history:
            role = item.get("role", "user")
            parts = item.get("parts", [])
            # Убедимся, что parts - это список
            if not isinstance(parts, list):
                parts = [parts]
            
            # Преобразуем PIL Image в Part, если необходимо
            processed_parts = []
            for part in parts:
                if isinstance(part, Image.Image): # Проверяем, является ли объект PIL Image
                    processed_parts.append(part)
                else:
                    processed_parts.append(types.Part.from_text(text=str(part)))
            
            contents.append(types.Content(role=role, parts=processed_parts))

        config = types.GenerateContentConfig(
            safety_settings=settings.SAFETY_SETTINGS
        )
        
        if system_instruction:
            config.system_instruction = system_instruction

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config
        )
        
        # Подсчет токенов
        token_count_response = client.models.count_tokens(
            model=model_name,
            contents=contents
        )
        
        return response.text, token_count_response.total_tokens
        
    except APIError as e:
        logging.error(f"Gemini API Error: {e}")
        if "quota" in str(e).lower():
            await metrics_collector.record_error("gemini_quota", str(e))
            return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
        else:
            await metrics_collector.record_error("gemini_api_call", str(e))
            return f"Произошла ошибка вызова API: {e}", None
    except Exception as e:
        logging.error(f"Gemini API generic error: {e}")
        await metrics_collector.record_error("gemini_api", str(e))
        return f"Произошла непредвиденная ошибка API: {e}", None

@NetworkErrorHandler.retry_with_backoff
async def _tavily_api_call(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Internal function for making Tavily API calls with retry logic."""
    response = await http_client.post("https://api.tavily.com/search", json=payload)
    response.raise_for_status()
    return response.json()

async def tavily_search_agent(query: str, search_type: str = "search"):
    # Проверяем кэш перед выполнением поиска
    cached_result = await get_cached_search_result(query, search_type)
    if cached_result:
        logging.info(f"Cache hit for Tavily search: {query[:50]}...")
        return cached_result
    
    available_key = await database.get_available_tavily_key()
    if not available_key:
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
    else:
        payload["search_depth"] = "advanced"
        payload["max_results"] = 7
        cost = settings.TAVILY_ADVANCED_SEARCH_COST

    try:
        data = await _tavily_api_call(payload)
        await database.increment_tavily_key_usage(available_key['key_hash'], cost)
        
        result = {}
        if search_type == "qna":
            result = {"type": "answer", "content": data.get("answer", "")}
        else:
            result = {"type": "search", "results": data.get('results', [])}
        
        # Сохраняем результат в кэш
        await cache_search_result(query, search_type, result)
        
        return result

    except httpx.HTTPStatusError as e:
        logging.error(f"Tavily API call failed with status {e.response.status_code}: {e.response.text}")
        await metrics_collector.record_error("tavily_http", f"Status {e.response.status_code}: {e.response.text}")
        return {"error": f"Ошибка API поиска: {e.response.status_code}. Убедитесь, что ключ API валиден."}
    except Exception as e:
        logging.error(f"Tavily API call failed: {e}")
        await metrics_collector.record_error("tavily_api", str(e))
        return {"error": f"Произошла ошибка во время вызова API поиска: {e}"}
