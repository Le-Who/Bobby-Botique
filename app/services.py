import logging
import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError
from typing import Dict, Any, List
from PIL import Image
import asyncio
import time

from app.config import settings
from app import database
from app.metrics import metrics_collector
from app.cache import get_cached_search_result, cache_search_result
from app.utils.network import NetworkErrorHandler
from app.utils.api_logger import api_logger

# Используем улучшенную конфигурацию HTTP клиента
http_client = NetworkErrorHandler.create_robust_http_client()

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None, user_id: int = None, chat_id: int = None):
    """
    Получает ответ от Gemini API с улучшенной обработкой ошибок.
    
    Args:
        api_key: API ключ для Gemini
        history: История сообщений
        model_name: Название модели
        system_instruction: Системная инструкция
        user_id: ID пользователя для логирования
        chat_id: ID чата для логирования
        
    Returns:
        Tuple (response_text, token_count) или (error_message, None)
    """
    # Валидация входных параметров
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")
    
    if not isinstance(history, list) or not history:
        raise ValueError("history must be a non-empty list")
    
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name must be a non-empty string")
    
    if user_id is not None and not isinstance(user_id, int):
        raise ValueError("user_id must be an integer")
    
    if chat_id is not None and not isinstance(chat_id, int):
        raise ValueError("chat_id must be an integer")
    
    # Инициализируем start_time по умолчанию
    start_time = None
    
    try:
        await metrics_collector.record_api_call("gemini", model_name)
        
        # Детальное логирование Gemini API запроса
        try:
            prompt_length = sum(len(str(part)) for item in history for part in (item.get("parts", []) or []) if part is not None)
            has_images = any(isinstance(part, Image.Image) for item in history for part in (item.get("parts", []) or []) if part is not None)
        except Exception as e:
            logging.warning(f"Error calculating prompt metrics: {e}, using fallback values")
            prompt_length = 0
            has_images = False
        
        start_time = api_logger.log_gemini_request(
            model=model_name,
            prompt_length=prompt_length,
            has_images=has_images,
            user_id=user_id,
            chat_id=chat_id
        )
        
        # Дополнительная проверка start_time
        if start_time is None or not isinstance(start_time, (int, float)):
            logging.warning(f"Invalid start_time returned from log_gemini_request: {start_time}, using current time")
            start_time = time.time()
        
        client = genai.Client(api_key=api_key)
        
        # Преобразуем историю в формат types.Content
        contents = []
        try:
            for item in history:
                if not isinstance(item, dict):
                    logging.warning(f"Skipping invalid history item (not dict): {type(item)}")
                    continue
                    
                role = item.get("role", "user")
                parts = item.get("parts", [])
                # Убедимся, что parts - это список
                if not isinstance(parts, list):
                    parts = [parts] if parts is not None else []
                elif parts is None:
                    parts = []
                
                # Преобразуем PIL Image в Part, если необходимо
                processed_parts = []
                for part in parts:
                    if isinstance(part, Image.Image): # Проверяем, является ли объект PIL Image
                        # Правильно создаем Part для изображения
                        # Конвертируем PIL Image в bytes для Gemini API
                        import io
                        img_byte_arr = io.BytesIO()
                        part.save(img_byte_arr, format='JPEG')
                        img_byte_arr = img_byte_arr.getvalue()
                        
                        try:
                            # Создаем Part для изображения используя правильный метод
                            # Согласно документации google-genai, используем inline_data
                            image_part = types.Part(
                                inline_data=types.Blob(
                                    mime_type="image/jpeg",
                                    data=img_byte_arr
                                )
                            )
                        except Exception as e:
                            logging.warning(f"Failed to create image part: {e}")
                            # Fallback: пропускаем изображение
                            logging.warning(f"Skipping image part due to creation error")
                            continue
                        
                        processed_parts.append(image_part)
                    else:
                        # Безопасное преобразование текста - убеждаемся, что это строка
                        try:
                            text_content = str(part)
                            processed_parts.append(types.Part.from_text(text=text_content))
                        except Exception as e:
                            logging.warning(f"Failed to process text part: {e}, skipping")
                            continue
                
                # Добавляем content только если есть обработанные parts
                if processed_parts and len(processed_parts) > 0:
                    try:
                        contents.append(types.Content(role=role, parts=processed_parts))
                    except Exception as e:
                        logging.warning(f"Failed to create Content object: {e}, skipping")
                        continue
        except Exception as e:
            logging.error(f"Error processing history: {e}")
            # Fallback: создаем простой content с ошибкой
            try:
                contents.append(types.Content(role="user", parts=[types.Part.from_text("Error processing request")]))
            except Exception as fallback_error:
                logging.error(f"Failed to create fallback content: {fallback_error}")
                return "❌ Ошибка обработки запроса", None

        # Проверяем, что contents не пустой
        if not contents or len(contents) == 0:
            error_msg = "Failed to create valid content for Gemini API - no valid parts found"
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_content_creation", error_msg)
            return f"❌ Ошибка создания контента для API: {error_msg}", None

        config = types.GenerateContentConfig(
            safety_settings=settings.SAFETY_SETTINGS
        )
        
        if system_instruction:
            try:
                # Убеждаемся, что system_instruction - это строка
                safe_system_instruction = str(system_instruction)
                config.system_instruction = safe_system_instruction
            except Exception as e:
                logging.warning(f"Failed to set system_instruction: {e}, continuing without it")

        # Выполняем запрос с timeout
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=contents,
                config=config
            ),
            timeout=60.0  # 60 секунд timeout
        )
        
        # Подсчет токенов с timeout
        try:
            token_count_response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.count_tokens,
                    model=model_name,
                    contents=contents
                ),
                timeout=10.0  # 10 секунд timeout
            )
        except Exception as token_error:
            logging.warning(f"Failed to count tokens: {token_error}, using fallback")
            # Создаем fallback объект для токенов
            class FallbackTokenCount:
                def __init__(self):
                    self.total_tokens = 0
            token_count_response = FallbackTokenCount()
        
        # Проверяем, что response.text не None перед логированием
        if response.text is None:
            error_msg = "Gemini API returned None response text"
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_none_response", error_msg)
            
            # Логируем ошибку
            if start_time is not None:
                api_logger.log_gemini_response(
                    start_time=start_time,
                    model=model_name,
                    response_length=0,
                    success=False,
                    error_message=error_msg,
                    user_id=user_id,
                    chat_id=chat_id
                )
            
            return "❌ API вернул пустой ответ. Попробуйте еще раз.", None
        
        # Логируем успешный ответ Gemini API
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=len(response.text),
                token_count=token_count_response.total_tokens,
                success=True,
                user_id=user_id,
                chat_id=chat_id
            )
        
        return response.text, token_count_response.total_tokens
        
    except asyncio.TimeoutError:
        error_msg = f"Gemini API request timed out for model {model_name}"
        logging.error(error_msg)
        await metrics_collector.record_error("gemini_timeout", error_msg)
        
        # Логируем ошибку timeout только если start_time был инициализирован
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=error_msg,
                user_id=user_id,
                chat_id=chat_id
            )
        
        return "⏰ Превышено время ожидания ответа от API. Попробуйте позже.", None
        
    except APIError as e:
        # Логируем ошибку Gemini API только если start_time был инициализирован
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=str(e),
                user_id=user_id,
                chat_id=chat_id
            )
        
        logging.error(f"Gemini API Error: {e}")
        if "quota" in str(e).lower():
            await metrics_collector.record_error("gemini_quota", str(e))
            return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
        else:
            await metrics_collector.record_error("gemini_api_call", str(e))
            return f"Произошла ошибка вызова API: {e}", None
            
    except Exception as e:
        # Логируем общую ошибку Gemini API только если start_time был инициализирован
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=str(e),
                user_id=user_id,
                chat_id=chat_id
            )
        
        logging.error(f"Gemini API generic error: {e}")
        await metrics_collector.record_error("gemini_api", str(e))
        return f"Произошла непредвиденная ошибка API: {e}", None

async def _tavily_api_call(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Internal function for making Tavily API calls with retry logic."""
    try:
        response = await http_client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Tavily API call error: {e}")
        raise

async def tavily_search_agent(query: str, search_type: str = "search", user_id: int = None, chat_id: int = None):
    # Валидация входных параметров
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must be a non-empty string")
    
    if query and len(query) > 1000:  # Ограничение длины запроса
        raise ValueError("Query too long. Maximum 1000 characters allowed")
    
    if search_type not in ["search", "qna"]:
        raise ValueError("search_type must be 'search' or 'qna'")
    
    # Валидация user_id и chat_id если они предоставлены
    if user_id is not None and (not isinstance(user_id, int) or user_id <= 0):
        raise ValueError("user_id must be a positive integer")
    
    if chat_id is not None and not isinstance(chat_id, int):
        raise ValueError("chat_id must be an integer")
    
    # Проверяем кэш перед выполнением поиска
    cached_result = await get_cached_search_result(query, search_type)
    if cached_result:
        logging.info(f"Cache hit for Tavily search: {query[:50]}...")
        return cached_result
    
    available_key = await database.get_available_tavily_key()
    if not available_key:
        return {"error": "Поиск недоступен: все API ключи сервиса поиска достигли месячного лимита."}
    
    api_key = available_key['api_key']
    
    # Детальное логирование Tavily API запроса
    start_time = api_logger.log_tavily_request(
        query=query,
        search_type=search_type,
        user_id=user_id,
        chat_id=chat_id
    )
    
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
        
        # Логируем успешный ответ Tavily API
        results = result.get('results', [])
        results_count = len(results) if results and result.get('type') == 'search' else 1
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=results_count,
            success=True,
            user_id=user_id,
            chat_id=chat_id
        )
        
        return result

    except httpx.HTTPStatusError as e:
        # Логируем ошибку Tavily API
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=0,
            success=False,
            error_message=f"HTTP {e.response.status_code}: {e.response.text}",
            user_id=user_id,
            chat_id=chat_id
        )
        
        logging.error(f"Tavily API call failed with status {e.response.status_code}: {e.response.text}")
        await metrics_collector.record_error("tavily_http", f"Status {e.response.status_code}: {e.response.text}")
        return {"error": f"Ошибка API поиска: {e.response.status_code}. Убедитесь, что ключ API валиден."}
    except Exception as e:
        # Логируем общую ошибку Tavily API
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=0,
            success=False,
            error_message=str(e),
            user_id=user_id,
            chat_id=chat_id
        )
        
        logging.error(f"Tavily API call failed: {e}")
        await metrics_collector.record_error("tavily_api", str(e))
        return {"error": f"Произошла непредвиденная ошибка API: {e}"}
