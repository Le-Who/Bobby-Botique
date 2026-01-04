import logging
import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError
from typing import Dict, Any, List, Optional
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

async def get_gemini_response(api_key: str, history: list, model_name: str, system_instruction: str = None, user_id: int = None, chat_id: int = None, max_retries: int = 3):
    """
    Получает ответ от Gemini API с улучшенной обработкой ошибок и retry механизмом.
    
    Args:
        api_key: API ключ для Gemini
        history: История сообщений
        model_name: Название модели
        system_instruction: Системная инструкция
        user_id: ID пользователя для логирования
        chat_id: ID чата для логирования
        max_retries: Максимальное количество попыток при ошибках 503
        
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
    
    # Retry механизм для ошибок 503
    for attempt in range(max_retries):
        try:
            return await _execute_gemini_request(api_key, history, model_name, system_instruction, user_id, chat_id)
        except Exception as e:
            error_message = str(e).lower()
            
            if ("503" in str(e) or "unavailable" in error_message or "overloaded" in error_message) and attempt < max_retries - 1:
                # Экспоненциальная задержка с максимумом 10 секунд
                wait_time = min(2 ** (attempt + 1), 10)
                logging.warning(f"Gemini API overloaded (attempt {attempt + 1}/{max_retries}). Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
                continue
            else:
                raise
    
    # Этот код не должен выполняться, но на всякий случай
    return "❌ Превышено максимальное количество попыток. Попробуйте позже.", None

async def _save_image_as_bytes(image: Image.Image, timeout: float = 5.0, max_size_mb: int = 10) -> Optional[bytes]:
    """Сохраняет изображение как bytes с timeout и сжатием."""
    import io, math
    try:
        # Проверяем размер в памяти
        img_bytes_approx = len(image.tobytes())
        if img_bytes_approx > max_size_mb * 1024 * 1024:
             # Уменьшаем
            ratio = math.sqrt((max_size_mb * 1024 * 1024) / img_bytes_approx)
            new_size = tuple(int(dim * ratio) for dim in image.size)
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        def _save():
            buf = io.BytesIO()
            image.save(buf, format='JPEG', quality=85, optimize=True)
            return buf.getvalue()

        return await asyncio.wait_for(asyncio.to_thread(_save), timeout=timeout)
    except Exception as e:
        logging.error(f"Image processing error: {e}")
        return None

async def _execute_gemini_request(api_key: str, history: list, model_name: str, system_instruction: str = None, user_id: int = None, chat_id: int = None):
    """
    Внутренняя функция для выполнения запроса к Gemini API.
    """
    # Гарантированная инициализация времени
    start_time = time.time()
    
    try:
        await metrics_collector.record_api_call("gemini", model_name)
        
        try:
            prompt_length = sum(len(str(part)) for item in history for part in (item.get("parts", []) or []) if part is not None)
            has_images = any(isinstance(part, Image.Image) for item in history for part in (item.get("parts", []) or []) if part is not None)
        except Exception as e:
            logging.warning(f"Metrics calc error: {e}")
            prompt_length = 0
            has_images = False
        
        # Логируем запрос (функция вернет start_time)
        start_time = api_logger.log_gemini_request(
            model=model_name,
            prompt_length=prompt_length,
            has_images=has_images,
            user_id=user_id,
            chat_id=chat_id
        )
        
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
                    if isinstance(part, Image.Image): 
                        # Используем безопасное сохранение с таймаутом
                        img_bytes = await _save_image_as_bytes(part)
                        
                        if img_bytes:
                            try:
                                image_part = types.Part(
                                    inline_data=types.Blob(
                                        mime_type="image/jpeg",
                                        data=img_bytes
                                    )
                                )
                                processed_parts.append(image_part)
                            except Exception as e:
                                logging.warning(f"Failed to create image part: {e}")
                        else:
                            logging.warning("Skipping image part due to processing error")
                        continue
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
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config
                ),
                timeout=120.0  # 120 секунд timeout (увеличено для медленных моделей)
            )
        except Exception as e:
            error_msg = f"Failed to generate content from Gemini API: {e}"
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_generation_failed", error_msg)
            
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
            
            return f"❌ Ошибка генерации ответа: {error_msg}", None
        
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
        
        # Дополнительная проверка response на None
        if not response or not hasattr(response, 'text'):
            error_msg = "Gemini API returned invalid response object"
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_invalid_response", error_msg)
            
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
            
            return "❌ API вернул некорректный ответ. Попробуйте еще раз.", None
        
        # Безопасная проверка и извлечение response.text
        response_text = response.text if response.text else ""
        if not response_text:
            error_msg = "Gemini API returned None or empty response text"
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
        
        # Логируем успешный ответ Gemini API (используем безопасную переменную)
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=len(response_text),
                token_count=token_count_response.total_tokens,
                success=True,
                user_id=user_id,
                chat_id=chat_id
            )
        
        return response_text, token_count_response.total_tokens
        
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
        
        # Обработка специфических ошибок
        error_message = str(e).lower()
        
        if "quota" in error_message:
            await metrics_collector.record_error("gemini_quota", str(e))
            return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
        elif "503" in str(e) or "unavailable" in error_message or "overloaded" in error_message:
            await metrics_collector.record_error("gemini_overloaded", str(e))
            return "🔄 Сервер Gemini перегружен. Попробуйте еще раз через несколько секунд.", None
        elif "invalid" in error_message or "malformed" in error_message:
            await metrics_collector.record_error("gemini_invalid_request", str(e))
            return "❌ Некорректный запрос к API. Проверьте параметры.", None
        elif "rate limit" in error_message:
            await metrics_collector.record_error("gemini_rate_limit", str(e))
            return "⏱️ Превышен лимит запросов в секунду. Подождите немного и попробуйте снова.", None
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

async def get_openrouter_response(api_key: str, history: list, model_name: str, system_instruction: str = None, user_id: int = None, chat_id: int = None, max_retries: int = 3):
    """
    Получает ответ от OpenRouter API с улучшенной обработкой ошибок и retry механизмом.
    
    Args:
        api_key: API ключ для OpenRouter
        history: История сообщений (в формате Gemini: [{'role': 'user', 'parts': [...]}])
        model_name: Название модели (например, "openai/gpt-4o")
        system_instruction: Системная инструкция
        user_id: ID пользователя для логирования
        chat_id: ID чата для логирования
        max_retries: Максимальное количество попыток при ошибках 503
        
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
    
    # Логируем входящие параметры
    logging.info(f"🔍 get_openrouter_response called: model={model_name}, system_instruction={'provided' if system_instruction else 'None'}, length={len(system_instruction) if system_instruction else 0}")
    
    # Retry механизм для ошибок 503
    for attempt in range(max_retries):
        try:
            return await _execute_openrouter_request(api_key, history, model_name, system_instruction, user_id, chat_id)
        except Exception as e:
            error_message = str(e).lower()
            
            # Если это ошибка 503 и у нас еще есть попытки, пробуем снова
            if ("503" in str(e) or "unavailable" in error_message or "overloaded" in error_message) and attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # Экспоненциальная задержка: 2, 4, 6 секунд
                logging.warning(f"OpenRouter API overloaded (attempt {attempt + 1}/{max_retries}). Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
                continue
            else:
                # Если это не 503 ошибка или попытки закончились, пробрасываем ошибку
                raise
    
    # Этот код не должен выполняться, но на всякий случай
    return "❌ Превышено максимальное количество попыток. Попробуйте позже.", None

async def _execute_openrouter_request(api_key: str, history: list, model_name: str, system_instruction: str = None, user_id: int = None, chat_id: int = None):
    """
    Внутренняя функция для выполнения запроса к OpenRouter API.
    """
    start_time = None
    
    try:
        await metrics_collector.record_api_call("openrouter", model_name)
        
        # Детальное логирование OpenRouter API запроса
        try:
            prompt_length = sum(len(str(part)) for item in history for part in (item.get("parts", []) or []) if part is not None)
            has_images = any(isinstance(part, Image.Image) for item in history for part in (item.get("parts", []) or []) if part is not None)
        except Exception as e:
            logging.warning(f"Error calculating prompt metrics: {e}, using fallback values")
            prompt_length = 0
            has_images = False
        
        # Используем тот же api_logger, но с типом "openrouter"
        start_time = time.time()
        
        # Преобразуем историю Gemini в формат OpenAI для OpenRouter
        messages = []
        
        # Добавляем системное сообщение, если есть
        # В OpenRouter системное сообщение должно быть первым в массиве messages
        if system_instruction:
            system_content = str(system_instruction).strip()
            if system_content:
                messages.append({
                    "role": "system",
                    "content": system_content
                })
                logging.info(f"✅ OpenRouter: Added system instruction (length: {len(system_content)}, preview: {system_content[:100]}...)")
            else:
                logging.warning(f"⚠️ OpenRouter: system_instruction is empty after strip()")
        else:
            logging.warning(f"⚠️ OpenRouter: system_instruction is None or falsy")
        
        # Преобразуем историю из формата Gemini в формат OpenAI
        for item in history:
            if not isinstance(item, dict):
                logging.warning(f"Skipping invalid history item (not dict): {type(item)}")
                continue
            
            role = item.get("role", "user")
            # В OpenRouter используем "assistant" вместо "model"
            if role == "model":
                role = "assistant"
            
            parts = item.get("parts", [])
            if not isinstance(parts, list):
                parts = [parts] if parts is not None else []
            elif parts is None:
                parts = []
            
            # Объединяем все части в один контент
            # Для изображений конвертируем в base64 (если нужно)
            content_parts = []
            for part in parts:
                if isinstance(part, Image.Image):
                    # Конвертируем изображение в base64
                    import io
                    import base64
                    img_byte_arr = io.BytesIO()
                    part.save(img_byte_arr, format='JPEG')
                    img_base64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_base64}"
                        }
                    })
                else:
                    # Текстовый контент
                    text_content = str(part)
                    if text_content.strip():
                        content_parts.append({
                            "type": "text",
                            "text": text_content
                        })
            
            # Если есть контент, добавляем сообщение
            if content_parts:
                # Если только один текстовый элемент, упрощаем формат
                if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                    messages.append({
                        "role": role,
                        "content": content_parts[0]["text"]
                    })
                else:
                    messages.append({
                        "role": role,
                        "content": content_parts
                    })
        
        if not messages:
            error_msg = "Failed to create valid messages for OpenRouter API"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_content_creation", error_msg)
            return f"❌ Ошибка создания контента для API: {error_msg}", None
        
        # Формируем запрос к OpenRouter API
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/your-repo",  # Опционально, для аналитики
            "X-Title": "GeminiBot v2"  # Опционально, для аналитики
        }
        
        payload = {
            "model": model_name,
            "messages": messages
        }
        
        # Логируем структуру сообщений для отладки
        has_system = len(messages) > 0 and messages[0].get("role") == "system"
        if has_system:
            system_content = messages[0].get('content', '')
            logging.info(f"✅ OpenRouter: Request includes system message (length: {len(system_content)}, first 200 chars: {system_content[:200]}...)")
        else:
            logging.warning(f"⚠️ OpenRouter: Request does NOT include system message! Total messages: {len(messages)}")
            if len(messages) > 0:
                logging.warning(f"⚠️ OpenRouter: First message role: {messages[0].get('role')}")
        logging.info(f"📤 OpenRouter: Sending request with {len(messages)} messages, model: {model_name}")
        
        # Выполняем запрос с timeout
        try:
            response = await asyncio.wait_for(
                http_client.post(url, json=payload, headers=headers),
                timeout=120.0  # 120 секунд timeout (увеличено для медленных моделей)
            )
            response.raise_for_status()
            response_data = response.json()
        except httpx.HTTPStatusError as e:
            error_msg = f"OpenRouter API HTTP error: {e.response.status_code} - {e.response.text}"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_http", error_msg)
            
            if start_time is not None:
                api_logger.log_gemini_response(  # Используем тот же логгер
                    start_time=start_time,
                    model=model_name,
                    response_length=0,
                    success=False,
                    error_message=error_msg,
                    user_id=user_id,
                    chat_id=chat_id
                )
            
            # Обработка специфических ошибок
            if e.response.status_code == 429:
                return "⏱️ Превышен лимит запросов. Подождите немного и попробуйте снова.", None
            elif e.response.status_code == 401:
                return "🔑 Неверный API ключ. Проверьте настройки.", None
            elif e.response.status_code == 402:
                return "💳 Недостаточно средств на счету OpenRouter.", None
            elif e.response.status_code == 503:
                return "🔄 Сервер OpenRouter перегружен. Попробуйте еще раз через несколько секунд.", None
            else:
                return f"❌ Ошибка API: {e.response.status_code}", None
        except asyncio.TimeoutError:
            error_msg = f"OpenRouter API request timed out for model {model_name}"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_timeout", error_msg)
            
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
        except Exception as e:
            error_msg = f"OpenRouter API error: {e}"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_api", error_msg)
            
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
            
            return f"❌ Ошибка API: {error_msg}", None
        
        # Извлекаем ответ
        if not response_data or "choices" not in response_data or not response_data["choices"]:
            error_msg = "OpenRouter API returned invalid response"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_invalid_response", error_msg)
            
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
            
            return "❌ API вернул некорректный ответ. Попробуйте еще раз.", None
        
        choice = response_data["choices"][0]
        message = choice.get("message", {})
        response_text = message.get("content", "")
        
        if not response_text:
            error_msg = "OpenRouter API returned empty response"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_empty_response", error_msg)
            
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
        
        # Подсчет токенов из ответа API (если доступно)
        usage = response_data.get("usage", {})
        token_count = usage.get("total_tokens", 0)
        
        # Логируем успешный ответ
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=len(response_text),
                token_count=token_count,
                success=True,
                user_id=user_id,
                chat_id=chat_id
            )
        
        return response_text, token_count
        
    except Exception as e:
        error_msg = f"OpenRouter API generic error: {e}"
        logging.error(error_msg)
        await metrics_collector.record_error("openrouter_api", error_msg)
        
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
        
        return f"❌ Произошла непредвиденная ошибка API: {e}", None
