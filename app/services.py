import logging
import asyncio
from tavily import TavilyClient
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from typing import Dict, Any, List

from . import config
from . import database

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

async def tavily_search_agent(query: str, search_type: str = "search", urls: List[str] = None):
    available_key = database.get_available_tavily_key()
    if not available_key:
        return {"error": "Поиск недоступен: все API ключи сервиса поиска достигли месячного лимита."}
    
    logging.info(f"Performing Tavily API call (type: {search_type}) for query: {query[:100]}")
    try:
        tavily = TavilyClient(api_key=available_key['api_key'])
        
        if search_type == "qna":
            response = await asyncio.to_thread(tavily.qna_search, query=query)
            database.increment_tavily_key_usage(available_key['key_hash'], config.TAVILY_QNA_SEARCH_COST)
            return {"type": "answer", "content": response}
        
        response = await asyncio.to_thread(
            tavily.search, query=query, search_depth="advanced", max_results=7
        )
        database.increment_tavily_key_usage(available_key['key_hash'], config.TAVILY_ADVANCED_SEARCH_COST)
        return {"type": "search", "results": response.get('results', [])}

    except Exception as e:
        logging.error(f"Tavily API call failed: {e}")
        return {"error": f"Произошла ошибка во время вызова API поиска: {e}"}
