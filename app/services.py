# app/services.py

import logging
import httpx
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import Dict, Any, List

from . import config
from . import database

# <<< ИЗМЕНЕНО: Создаем один клиент на все время жизни приложения
http_client = httpx.AsyncClient(timeout=30.0)

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None):
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name, safety_settings=config.SAFETY_SETTINGS, system_instruction=system_instruction)
        chat = model.start_chat(history=history[:-1])
        response = await chat.send_message_async(history[-1]['parts'])
        token_count = model.count_tokens(chat.history).total_tokens
        return response.text, token_count
    except google_exceptions.ResourceExhausted as e:
        logging.error(f"Gemini API Quota Error: {e}")
        return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
    except Exception as e:
        logging.error(f"Gemini API generic error: {e}")
        return f"Произошла непредвиденная ошибка API: {e}", None

# <<< ИЗМЕНЕНО: Полностью переписано на httpx
async def tavily_search_agent(query: str, search_type: str = "search"):
    available_key = await database.get_available_tavily_key()
    if not available_key:
        return {"error": "Поиск недоступен: все API ключи сервиса поиска достигли месячного лимита."}
    
    api_key = available_key['api_key']
    logging.info(f"Performing Tavily API call (type: {search_type}) for query: {query[:100]}")
    
    payload = {
        "api_key": api_key,
        "query": query,
    }
    cost = 0

    if search_type == "qna":
        payload["search_depth"] = "basic"
        cost = config.TAVILY_QNA_SEARCH_COST
    else: # "search"
        payload["search_depth"] = "advanced"
        payload["max_results"] = 7
        cost = config.TAVILY_ADVANCED_SEARCH_COST

    try:
        response = await http_client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        
        data = response.json()
        await database.increment_tavily_key_usage(available_key['key_hash'], cost)
        
        if search_type == "qna":
            return {"type": "answer", "content": data.get("answer", "")}
        
        return {"type": "search", "results": data.get('results', [])}

    except httpx.HTTPStatusError as e:
        logging.error(f"Tavily API call failed with status {e.response.status_code}: {e.response.text}")
        return {"error": f"Ошибка API поиска: {e.response.status_code}. Убедитесь, что ключ API валиден."}
    except Exception as e:
        logging.error(f"Tavily API call failed: {e}")
        return {"error": f"Произошла ошибка во время вызова API поиска: {e}"}
