import logging
import warnings
import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError
from typing import Dict, Any, Optional, Union
from PIL import Image
import asyncio
import time
import io
import math
import base64

from app.config import settings
from app import database
from app.metrics import metrics_collector
from app.cache import get_cached_search_result, cache_search_result
from app.request_context import get_request_id
from app.utils.network import NetworkErrorHandler
from app.utils.api_logger import api_logger
from app.utils.image import estimate_image_size_in_bytes

import concurrent.futures

# Используем улучшенную конфигурацию HTTP клиента
http_client = NetworkErrorHandler.create_robust_http_client()

# Глобальный пул процессов for обработки fromображений вне GIL
_image_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=2)


# ─── Shared helpers ──────────────────────────────────────────────────────────


def _validate_api_inputs(
    api_key: str,
    history: list,
    model_name: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
) -> None:
    """Common input validation for all AI provider functions."""
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


def _caller_info() -> str:
    """Return caller filename:lineno for deprecation tracking."""
    import inspect
    frame = inspect.currentframe()
    try:
        # Walk two frames up: _caller_info -> deprecated func -> actual caller
        caller = frame.f_back.f_back if frame and frame.f_back else None
        if caller:
            fname = caller.f_code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
            return f"{fname}:{caller.f_lineno}"
        return "unknown"
    finally:
        del frame


async def _with_retry(
    provider_name: str,
    execute_fn,
    max_retries: int = 3,
    *,
    non_retryable_msg: str = "❌ Ошибка вызова API: {error}",
):
    """Shared retry wrapper with exponential backoff for transient errors."""
    for attempt in range(max_retries):
        try:
            return await execute_fn()
        except Exception as e:
            error_text = str(e).lower()
            is_transient = (
                "503" in str(e)
                or "unavailable" in error_text
                or "overloaded" in error_text
            )
            if is_transient and attempt < max_retries - 1:
                wait_time = min(2 ** (attempt + 1), 10)
                logging.warning(
                    f"{provider_name} API overloaded (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {wait_time}s..."
                )
                await asyncio.sleep(wait_time)
                continue
            elif is_transient:
                logging.error("Max retries exceeded for %s API: %s", provider_name, e)
                continue
            else:
                return non_retryable_msg.format(error=e), None

    return "❌ Превышено максимальное количество попыток. Попробуйте позже.", None


def _image_worker(
    image_data: Union[bytes, Image.Image], max_size_mb: int = 10
) -> Optional[bytes]:
    import io
    from PIL import Image
    import math

    try:
        from app.utils.image import estimate_image_size_in_bytes

        if isinstance(image_data, bytes):
            img_to_process = Image.open(io.BytesIO(image_data))
        else:
            img_to_process = image_data

        # Use optimized estimation
        img_bytes_approx = estimate_image_size_in_bytes(img_to_process)

        if img_bytes_approx > max_size_mb * 1024 * 1024:
            ratio = math.sqrt((max_size_mb * 1024 * 1024) / img_bytes_approx)
            new_size = tuple(int(dim * ratio) for dim in img_to_process.size)
            img_to_process = img_to_process.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        if img_to_process.mode in ("RGBA", "P"):
            img_to_process = img_to_process.convert("RGB")

        img_to_process.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logging.error("Error in image processing worker: %s", e, exc_info=True)
        return None


async def get_gemini_response(
    api_key: str,
    history: list,
    model_name: str,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
    max_retries: int = 3,
):
    """
    Получает response от Gemini API with retry mechanism.

    Returns:
        Tuple (response_text, token_count) or (error_message, None)
    """
    warnings.warn(
        "get_gemini_response() is deprecated. Use GeminiProvider._execute_request() "
        "or ProviderRouter.get_response() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    logging.warning(
        "DEPRECATION: get_gemini_response() called by %s — migrate to Provider classes",
        _caller_info(),
    )
    _validate_api_inputs(api_key, history, model_name, user_id, chat_id)

    async def _attempt():
        return await _execute_gemini_request(
            api_key, history, model_name, system_instruction, user_id, chat_id
        )

    return await _with_retry("Gemini", _attempt, max_retries)


async def _save_image_as_bytes(
    image_data: Union[bytes, Image.Image], timeout: float = 5.0, max_size_mb: int = 10
) -> Optional[bytes]:
    """Сохраняет изображение как bytes с timeout и сжатием вне GIL."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _image_process_pool, _image_worker, image_data, max_size_mb
            ),
            timeout=timeout,
        )
    except Exception as e:
        logging.error("Image processing error: %s", e)
        return None


async def _execute_gemini_request(
    api_key: str,
    history: list,
    model_name: str,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
):
    """
    Internal function for выполнения requestа к Gemini API.
    """
    # Guaranteed initialization времени
    start_time = time.time()

    try:
        await metrics_collector.record_api_call("gemini", model_name)

        try:
            prompt_length = sum(
                len(str(part))
                for item in history
                for part in (item.get("parts", []) or [])
                if part is not None
            )
            has_images = any(
                isinstance(part, (bytes, bytearray, Image.Image))
                for item in history
                for part in (item.get("parts", []) or [])
                if part is not None
            )
        except Exception as e:
            logging.warning("Metrics calc error: %s", e)
            prompt_length = 0
            has_images = False

        # Log request (функция вернет start_time)
        start_time = api_logger.log_gemini_request(
            model=model_name,
            prompt_length=prompt_length,
            has_images=has_images,
            user_id=user_id,
            chat_id=chat_id,
        )
        request_id = get_request_id()
        client_kwargs = {"api_key": api_key}
        # SDK-level HTTP timeout prevents zombie connections if the model
        # takes too long to respond (e.g. gemini-2.5-flash "thinking" models)
        http_opts = {"timeout": 90_000}  # 90s in milliseconds
        if request_id:
            http_opts["headers"] = {"X-Request-ID": request_id}
        client_kwargs["http_options"] = types.HttpOptions(**http_opts)

        client = genai.Client(**client_kwargs)
        # Convert history to format types.Content
        contents = []
        try:
            for item in history:
                if not isinstance(item, dict):
                    logging.warning(
                        f"Skipping invalid history item (not dict): {type(item)}"
                    )
                    continue

                role = item.get("role", "user")
                parts = item.get("parts", [])
                # Ensure parts is a list
                if not isinstance(parts, list):
                    parts = [parts] if parts is not None else []
                elif parts is None:
                    parts = []

                # Convert PIL Image в Part, if необходимо
                processed_parts = []
                for part in parts:
                    if isinstance(part, (bytes, bytearray, Image.Image)):
                        # Use safe save with timeout
                        # Image.Image passed directly for processing in process pool
                        img_bytes = await _save_image_as_bytes(part)

                        if img_bytes:
                            try:
                                image_part = types.Part(
                                    inline_data=types.Blob(
                                        mime_type="image/jpeg", data=img_bytes
                                    )
                                )
                                processed_parts.append(image_part)
                            except Exception as e:
                                logging.warning("Failed to create image part: %s", e)
                        else:
                            logging.warning(
                                "Skipping image part due to processing error"
                            )
                        continue
                    else:
                        # Safe text conversion - ensure, что это строка
                        try:
                            text_content = str(part)
                            processed_parts.append(
                                types.Part.from_text(text=text_content)
                            )
                        except Exception as e:
                            logging.warning(
                                f"Failed to process text part: {e}, skipping"
                            )
                            continue

                # Add content only if processed parts exist
                if processed_parts and len(processed_parts) > 0:
                    try:
                        contents.append(types.Content(role=role, parts=processed_parts))
                    except Exception as e:
                        logging.warning(
                            f"Failed to create Content object: {e}, skipping"
                        )
                        continue
        except Exception as e:
            logging.error("Error processing history: %s", e)
            # Fallback: create simple error content
            try:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text("Error processing request")],
                    )
                )
            except Exception as fallback_error:
                logging.error("Failed to create fallback content: %s", fallback_error)
                return "❌ Ошибка обработки запроса", None

        # Check, что contents не empty
        if not contents or len(contents) == 0:
            error_msg = (
                "Failed to create valid content for Gemini API - no valid parts found"
            )
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_content_creation", error_msg)
            return f"❌ Ошибка создания контента для API: {error_msg}", None

        config = types.GenerateContentConfig(safety_settings=settings.SAFETY_SETTINGS)

        if system_instruction:
            try:
                # Убеждаемся, что system_instruction - это строка
                safe_system_instruction = str(system_instruction)
                config.system_instruction = safe_system_instruction
            except Exception as e:
                logging.warning(
                    f"Failed to set system_instruction: {e}, continuing without it"
                )

        # Native async call — properly supports cancellation via CancelledError.
        # The old asyncio.to_thread(client.models.generate_content, ...) approach
        # spawned a thread that kept running even after asyncio.wait_for cancelled
        # the future, causing zombie requests on timeout with "thinking" models.
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            ),
            timeout=100.0,  # 100s Python-side deadline (SDK has 90s HTTP timeout)
        )

        # Token counting — also native async
        try:
            token_count_response = await asyncio.wait_for(
                client.aio.models.count_tokens(
                    model=model_name, contents=contents
                ),
                timeout=10.0,
            )
        except Exception as token_error:
            logging.warning("Failed to count tokens: %s, using fallback", token_error)

            class FallbackTokenCount:
                def __init__(self):
                    self.total_tokens = 0

            token_count_response = FallbackTokenCount()

        # Additional check for None response
        if not response or not hasattr(response, "text"):
            error_msg = "Gemini API returned invalid response object"
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_invalid_response", error_msg)

            # Log error
            if start_time is not None:
                api_logger.log_gemini_response(
                    start_time=start_time,
                    model=model_name,
                    response_length=0,
                    success=False,
                    error_message=error_msg,
                    user_id=user_id,
                    chat_id=chat_id,
                )

            return "❌ API вернул некорректный ответ. Попробуйте еще раз.", None

        # Safe check and extraction response.text
        response_text = response.text if response.text else ""
        if not response_text:
            error_msg = "Gemini API returned None or empty response text"
            logging.error(error_msg)
            await metrics_collector.record_error("gemini_none_response", error_msg)

            # Log error
            if start_time is not None:
                api_logger.log_gemini_response(
                    start_time=start_time,
                    model=model_name,
                    response_length=0,
                    success=False,
                    error_message=error_msg,
                    user_id=user_id,
                    chat_id=chat_id,
                )

            return "❌ API вернул пустой ответ. Попробуйте еще раз.", None

        # Log успешный response Gemini API (use withoutопасную переменную)
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=len(response_text),
                token_count=token_count_response.total_tokens,
                success=True,
                user_id=user_id,
                chat_id=chat_id,
            )

        return response_text, token_count_response.total_tokens

    except asyncio.TimeoutError:
        error_msg = f"Gemini API request timed out for model {model_name}"
        logging.error(error_msg)
        await metrics_collector.record_error("gemini_timeout", error_msg)

        # Log error timeout only if start_time был инициалfromирован
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=error_msg,
                user_id=user_id,
                chat_id=chat_id,
            )

        return "⏰ Превышено время ожидания ответа от API. Попробуйте позже.", None

    except APIError as e:
        # Log error Gemini API only if start_time был инициалfromирован
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=str(e),
                user_id=user_id,
                chat_id=chat_id,
            )

        logging.error("Gemini API Error: %s", e)

        # Handle specific errors
        error_message = str(e).lower()

        if "quota" in error_message:
            await metrics_collector.record_error("gemini_quota", str(e))
            return "🚫 Достигнут лимит запросов к API (Quota Exceeded).", None
        elif (
            "503" in str(e)
            or "unavailable" in error_message
            or "overloaded" in error_message
        ):
            await metrics_collector.record_error("gemini_overloaded", str(e))
            # Raise exception to trigger retry mechanism in get_gemini_response
            raise
        elif "invalid" in error_message or "malformed" in error_message:
            await metrics_collector.record_error("gemini_invalid_request", str(e))
            return "❌ Некорректный запрос к API. Проверьте параметры.", None
        elif "rate limit" in error_message:
            await metrics_collector.record_error("gemini_rate_limit", str(e))
            return (
                "⏱️ Превышен лимит запросов в секунду. Подождите немного и попробуйте снова.",
                None,
            )
        else:
            await metrics_collector.record_error("gemini_api_call", str(e))
            return f"Произошла ошибка вызова API: {e}", None

    except Exception as e:
        # Log общую ошибку Gemini API only if start_time был инициалfromирован
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=str(e),
                user_id=user_id,
                chat_id=chat_id,
            )

        logging.error("Gemini API generic error: %s", e, exc_info=True)
        await metrics_collector.record_error("gemini_api", str(e))
        return f"Произошла непредвиденная ошибка API: {e}", None


async def _tavily_api_call(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Internal function for making Tavily API calls with retry logic."""
    try:
        headers = {}
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id

        response = await http_client.post(
            "https://api.tavily.com/search", json=payload, headers=headers or None
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error("Tavily API call error: %s", e)
        raise


async def tavily_search_agent(
    query: str, search_type: str = "search", user_id: int = None, chat_id: int = None
):
    # Validation входных parameterов
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must be a non-empty string")

    if query and len(query) > 1000:  # Ограничение длины requestа
        raise ValueError("Query too long. Maximum 1000 characters allowed")

    if search_type not in ["search", "qna"]:
        raise ValueError("search_type must be 'search' or 'qna'")

    # Validation user_id и chat_id if они предоставлены
    if user_id is not None and (not isinstance(user_id, int) or user_id <= 0):
        raise ValueError("user_id must be a positive integer")

    if chat_id is not None and not isinstance(chat_id, int):
        raise ValueError("chat_id must be an integer")

    # Check cache before выполнением searchа
    cached_result = await get_cached_search_result(query, search_type)
    if cached_result:
        logging.info("Cache hit for Tavily search: %s...", query[:50])
        return cached_result

    available_key = await database.get_available_tavily_key()
    if not available_key:
        return {
            "error": "Поиск недоступен: все API ключи сервиса поиска достигли месячного лимита."
        }

    api_key = available_key["api_key"]

    # Детальное логирование Tavily API requestа
    start_time = api_logger.log_tavily_request(
        query=query, search_type=search_type, user_id=user_id, chat_id=chat_id
    )

    logging.info(
        f"Performing Tavily API call (type: {search_type}) for query: {query[:100]}"
    )

    # Write metrics searchового requestа
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
        await database.increment_tavily_key_usage(available_key["key_hash"], cost)

        result = {}
        if search_type == "qna":
            result = {"type": "answer", "content": data.get("answer", "")}
        else:
            result = {"type": "search", "results": data.get("results", [])}

        # Save result в cache
        await cache_search_result(query, search_type, result)

        # Log успешный response Tavily API
        results = result.get("results", [])
        results_count = (
            len(results) if results and result.get("type") == "search" else 1
        )
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=results_count,
            success=True,
            user_id=user_id,
            chat_id=chat_id,
        )

        return result

    except httpx.HTTPStatusError as e:
        # Log error Tavily API
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=0,
            success=False,
            error_message=f"HTTP {e.response.status_code}: {e.response.text}",
            user_id=user_id,
            chat_id=chat_id,
        )

        logging.error(
            f"Tavily API call failed with status {e.response.status_code}: {e.response.text}"
        )
        await metrics_collector.record_error(
            "tavily_http", f"Status {e.response.status_code}: {e.response.text}"
        )
        return {
            "error": f"Ошибка API поиска: {e.response.status_code}. Убедитесь, что ключ API валиден."
        }
    except Exception as e:
        # Log общую ошибку Tavily API
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=0,
            success=False,
            error_message=str(e),
            user_id=user_id,
            chat_id=chat_id,
        )

        logging.error("Tavily API call failed: %s", e, exc_info=True)
        await metrics_collector.record_error("tavily_api", str(e))
        return {"error": f"Произошла непредвиденная ошибка API: {e}"}


async def get_openrouter_response(
    api_key: str,
    history: list,
    model_name: str,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
    max_retries: int = 3,
):
    """
    Получает response от OpenRouter API with retry mechanism.

    Returns:
        Tuple (response_text, token_count) or (error_message, None)
    """
    warnings.warn(
        "get_openrouter_response() is deprecated. Use OpenRouterProvider._execute_request() "
        "or ProviderRouter.get_response() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    logging.warning(
        "DEPRECATION: get_openrouter_response() called by %s — migrate to Provider classes",
        _caller_info(),
    )
    _validate_api_inputs(api_key, history, model_name, user_id, chat_id)

    async def _attempt():
        return await _execute_openrouter_request(
            api_key, history, model_name, system_instruction, user_id, chat_id
        )

    return await _with_retry("OpenRouter", _attempt, max_retries)


async def _execute_openrouter_request(
    api_key: str,
    history: list,
    model_name: str,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
):
    """
    Internal function for выполнения requestа к OpenRouter API.
    """
    start_time = None

    try:
        await metrics_collector.record_api_call("openrouter", model_name)

        start_time = time.time()

        # Convert history Gemini to format OpenAI for OpenRouter
        messages = []

        # System message first
        if system_instruction:
            system_content = str(system_instruction).strip()
            if system_content:
                messages.append({"role": "system", "content": system_content})

        # Convert history from format Gemini to format OpenAI
        for item in history:
            if not isinstance(item, dict):
                logging.warning(
                    f"Skipping invalid history item (not dict): {type(item)}"
                )
                continue

            role = item.get("role", "user")
            # В OpenRouter use "assistant" instead of "model"
            if role == "model":
                role = "assistant"

            parts = item.get("parts", [])
            if not isinstance(parts, list):
                parts = [parts] if parts is not None else []
            elif parts is None:
                parts = []

            # Объединяем все части в один content
            # Для fromображений конвертируем в base64 (if нужно)
            content_parts = []
            for part in parts:
                if isinstance(part, (bytes, bytearray, Image.Image)):
                    # Use offloaded processing
                    # Image.Image passed directly for processing in process pool
                    img_bytes = await _save_image_as_bytes(part)
                    if img_bytes:
                        # base64 encoding in thread to prevent blocking
                        def _encode():
                            return base64.b64encode(img_bytes).decode("utf-8")

                        img_base64 = await asyncio.to_thread(_encode)
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{img_base64}"
                                },
                            }
                        )
                else:
                    # Text content
                    text_content = str(part)
                    if text_content.strip():
                        content_parts.append({"type": "text", "text": text_content})

            # If content exists, add message
            if content_parts:
                # If only one text element, simplify format
                if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                    messages.append({"role": role, "content": content_parts[0]["text"]})
                else:
                    messages.append({"role": role, "content": content_parts})

        if not messages:
            error_msg = "Failed to create valid messages for OpenRouter API"
            logging.error(error_msg)
            await metrics_collector.record_error(
                "openrouter_content_creation", error_msg
            )
            return f"❌ Ошибка создания контента для API: {error_msg}", None

        # Build request к OpenRouter API
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/your-repo",  # Опционально, для аналитики
            "X-Title": "GeminiBot v2",  # Опционально, для аналитики
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id

        payload = {"model": model_name, "messages": messages}
        logging.debug(
            f"OpenRouter: sending {len(messages)} messages to {model_name}"
        )

        # OpenRouter uses httpx.AsyncClient (already async — no zombie-thread risk).
        # The httpx client has a 30s read timeout as the primary deadline.
        # This asyncio.wait_for is a safety net for extreme edge cases.
        try:
            response = await asyncio.wait_for(
                http_client.post(url, json=payload, headers=headers),
                timeout=90.0,
            )
            response.raise_for_status()
            response_data = response.json()
        except httpx.HTTPStatusError as e:
            error_msg = f"OpenRouter API HTTP error: {e.response.status_code} - {e.response.text}"
            logging.error(error_msg)
            await metrics_collector.record_error("openrouter_http", error_msg)

            if start_time is not None:
                api_logger.log_gemini_response(  # Using the same logger
                    start_time=start_time,
                    model=model_name,
                    response_length=0,
                    success=False,
                    error_message=error_msg,
                    user_id=user_id,
                    chat_id=chat_id,
                )

            # Handle specific errors
            if e.response.status_code == 429:
                return (
                    "⏱️ Превышен лимит запросов. Подождите немного и попробуйте снова.",
                    None,
                )
            elif e.response.status_code == 401:
                return "🔑 Неверный API ключ. Проверьте настройки.", None
            elif e.response.status_code == 402:
                return "💳 Недостаточно средств на счету OpenRouter.", None
            elif e.response.status_code == 503:
                return (
                    "🔄 Сервер OpenRouter перегружен. Попробуйте еще раз через несколько секунд.",
                    None,
                )
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
                    chat_id=chat_id,
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
                    chat_id=chat_id,
                )

            return f"❌ Ошибка API: {error_msg}", None

        # Extract response
        if (
            not response_data
            or "choices" not in response_data
            or not response_data["choices"]
        ):
            error_msg = "OpenRouter API returned invalid response"
            logging.error(error_msg)
            await metrics_collector.record_error(
                "openrouter_invalid_response", error_msg
            )

            if start_time is not None:
                api_logger.log_gemini_response(
                    start_time=start_time,
                    model=model_name,
                    response_length=0,
                    success=False,
                    error_message=error_msg,
                    user_id=user_id,
                    chat_id=chat_id,
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
                    chat_id=chat_id,
                )

            return "❌ API вернул пустой ответ. Попробуйте еще раз.", None

        # Подсчет tokenов from responseа API (if доступно)
        usage = response_data.get("usage", {})
        token_count = usage.get("total_tokens", 0)

        # Log успешный response
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=len(response_text),
                token_count=token_count,
                success=True,
                user_id=user_id,
                chat_id=chat_id,
            )

        return response_text, token_count

    except Exception as e:
        logging.error("OpenRouter API generic error: %s", e, exc_info=True)
        await metrics_collector.record_error("openrouter_api", str(e))

        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time,
                model=model_name,
                response_length=0,
                success=False,
                error_message=str(e),
                user_id=user_id,
                chat_id=chat_id,
            )

        return f"❌ Произошла непредвиденная ошибка API: {e}", None
