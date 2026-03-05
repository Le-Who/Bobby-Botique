import asyncio
import json
import logging
import os
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any

from app.request_context import get_request_id


class APILogger:
    """
    Детальное логирование for всех API requestов (Telegram, Gemini, Tavily)
    """

    def __init__(self):
        self.logger = logging.getLogger("api_logger")
        self.logger.setLevel(logging.INFO)
        self._pretty = os.environ.get("LOG_PRETTY", "").lower() in (
            "1",
            "true",
            "yes",
        )
        # No handler added here — api_logger propagates through the root
        # logger's handler so all output goes to a single stream (stdout)
        # with consistent formatting (Rich / JSON / plain).

    def _format_log(self, data: dict) -> str:
        """Format log payload — pretty-printed in dev, compact in prod."""
        if self._pretty:
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        return json.dumps(data, ensure_ascii=False, default=str)

    def log_api_request(
        self,
        api_name: str,
        endpoint: str,
        method: str = "GET",
        request_data: dict[str, Any] | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Логирует начало API запроса"""
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "endpoint": endpoint,
            "method": method,
            "request_id": get_request_id(),
            "user_id": user_id,
            "chat_id": chat_id,
            "request_data": self._sanitize_data(request_data),
            "status": "STARTED",
        }

        self.logger.info(f"🚀 API REQUEST STARTED: {self._format_log(log_data)}")
        return time.time()

    def log_api_response(
        self,
        api_name: str,
        endpoint: str,
        start_time: float,
        response_data: dict[str, Any] | None = None,
        status_code: int | None = None,
        success: bool = True,
        error_message: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Логирует завершение API запроса"""
        duration = time.time() - start_time

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "endpoint": endpoint,
            "duration_ms": round(duration * 1000, 2),
            "request_id": get_request_id(),
            "status_code": status_code,
            "success": success,
            "user_id": user_id,
            "chat_id": chat_id,
            "response_summary": self._summarize_response(response_data),
            "error_message": error_message,
            "status": "COMPLETED",
        }

        if success:
            self.logger.info(f"✅ API REQUEST COMPLETED: {self._format_log(log_data)}")
        else:
            self.logger.error(f"❌ API REQUEST FAILED: {self._format_log(log_data)}")

        return duration

    def log_gemini_request(
        self,
        model: str,
        prompt_length: int,
        has_images: bool = False,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Специальное логирование для Gemini API"""
        try:
            start_time = time.time()

            log_data = {
                "timestamp": datetime.now().isoformat(),
                "api": "gemini",
                "model": model,
                "prompt_length": prompt_length,
                "has_images": has_images,
                "request_id": get_request_id(),
                "user_id": user_id,
                "chat_id": chat_id,
                "status": "STARTED",
            }

            self.logger.info(f"🤖 GEMINI REQUEST STARTED: {self._format_log(log_data)}")
            return start_time

        except Exception as e:
            # Log error логирования, но не прерываем выполнение
            logging.error("Error in log_gemini_request: %s", e, exc_info=True)
            # Return текущее время как fallback
            return time.time()

    def log_provider_response(
        self,
        provider: str,
        start_time: float,
        model: str,
        response_length: int,
        token_count: int | None = None,
        success: bool = True,
        error_message: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Unified provider response logger (Gemini, OpenRouter, etc.)."""
        try:
            if not isinstance(start_time, (int, float)) or start_time <= 0:
                logging.warning("Invalid start_time in log_%s_response: %s, using current time", provider, start_time)
                start_time = time.time()

            duration = time.time() - start_time
            label = provider.upper()

            log_data = {
                "timestamp": datetime.now().isoformat(),
                "api": provider,
                "model": model,
                "duration_ms": round(duration * 1000, 2),
                "request_id": get_request_id(),
                "response_length": response_length,
                "token_count": token_count,
                "success": success,
                "user_id": user_id,
                "chat_id": chat_id,
                "error_message": error_message,
                "status": "COMPLETED",
            }

            if success:
                self.logger.info(f"✅ {label} RESPONSE COMPLETED: {self._format_log(log_data)}")
            else:
                self.logger.error(f"❌ {label} RESPONSE FAILED: {self._format_log(log_data)}")

            return duration

        except Exception as e:
            logging.error("Error in log_%s_response: %s", provider, e, exc_info=True)
            return 0.0

    # Backward-compat delegates
    def log_gemini_response(self, **kwargs):
        """Логирует ответ Gemini API."""
        return self.log_provider_response(provider="gemini", **kwargs)

    def log_openrouter_response(self, **kwargs):
        """Логирует ответ OpenRouter API."""
        return self.log_provider_response(provider="openrouter", **kwargs)

    def log_tavily_request(
        self,
        query: str,
        search_type: str,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Специальное логирование для Tavily API"""
        start_time = time.time()

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "tavily",
            "search_type": search_type,
            "query_length": len(query),
            "query_preview": query[:100] + "..." if len(query) > 100 else query,
            "user_id": user_id,
            "chat_id": chat_id,
            "status": "STARTED",
        }

        self.logger.info(f"🔍 TAVILY REQUEST STARTED: {self._format_log(log_data)}")
        return start_time

    def log_tavily_response(
        self,
        start_time: float,
        search_type: str,
        results_count: int,
        success: bool = True,
        error_message: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Логирует ответ Tavily API"""
        duration = time.time() - start_time

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "tavily",
            "search_type": search_type,
            "duration_ms": round(duration * 1000, 2),
            "results_count": results_count,
            "success": success,
            "user_id": user_id,
            "chat_id": chat_id,
            "error_message": error_message,
            "status": "COMPLETED",
        }

        if success:
            self.logger.info(f"✅ TAVILY RESPONSE COMPLETED: {self._format_log(log_data)}")
        else:
            self.logger.error(f"❌ TAVILY RESPONSE FAILED: {self._format_log(log_data)}")

        return duration

    def log_telegram_request(
        self,
        method: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        message_type: str | None = None,
    ):
        """Специальное логирование для Telegram Bot API"""
        start_time = time.time()

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "telegram",
            "method": method,
            "chat_id": chat_id,
            "user_id": user_id,
            "message_type": message_type,
            "status": "STARTED",
        }

        self.logger.info(f"📱 TELEGRAM REQUEST STARTED: {self._format_log(log_data)}")
        return start_time

    def log_telegram_response(
        self,
        start_time: float,
        method: str,
        success: bool = True,
        error_message: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
    ):
        """Логирует ответ Telegram Bot API"""
        duration = time.time() - start_time

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "telegram",
            "method": method,
            "duration_ms": round(duration * 1000, 2),
            "success": success,
            "chat_id": chat_id,
            "user_id": user_id,
            "error_message": error_message,
            "status": "COMPLETED",
        }

        if success:
            self.logger.info(f"✅ TELEGRAM RESPONSE COMPLETED: {self._format_log(log_data)}")
        else:
            self.logger.error(f"❌ TELEGRAM RESPONSE FAILED: {self._format_log(log_data)}")

        return duration

    def log_error(
        self,
        api_name: str,
        error: Exception,
        context: dict[str, Any] | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Логирует ошибки API с полным стектрейсом"""
        error_data = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "request_id": get_request_id(),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc(),
            "context": context,
            "user_id": user_id,
            "chat_id": chat_id,
            "status": "ERROR",
        }

        self.logger.error(f"💥 API ERROR: {self._format_log(error_data)}")

    def _sanitize_data(self, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """Очищает чувствительные данные из логов"""
        if not data:
            return None

        sanitized = data.copy()
        sensitive_keys = ["api_key", "token", "password", "secret"]

        for key in sensitive_keys:
            if key in sanitized:
                if isinstance(sanitized[key], str) and len(sanitized[key]) > 8:
                    sanitized[key] = sanitized[key][:4] + "..." + sanitized[key][-4:]
                else:
                    sanitized[key] = "***"

        return sanitized

    def _summarize_response(self, response_data: dict[str, Any] | None) -> dict[str, Any] | None:
        """Создает краткое описание ответа"""
        if not response_data:
            return None

        summary = {}

        if isinstance(response_data, dict):
            # Подсчитываем размер responseа
            if "text" in response_data and response_data["text"] is not None:
                summary["text_length"] = len(str(response_data["text"]))
            if "results" in response_data and response_data["results"] is not None:
                summary["results_count"] = len(response_data["results"])
            if "content" in response_data and response_data["content"] is not None:
                summary["content_length"] = len(str(response_data["content"]))

        return summary


# Глобальный экземпляр логгера
api_logger = APILogger()


def log_api_call(api_name: str, endpoint: str = ""):
    """Декоратор для логирования API вызовов"""

    def decorator(func: Callable):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Extract user_id и chat_id from argumentов if возможно
            user_id = None
            chat_id = None

            # Ищем объекты Update or Message в argumentах
            for arg in args:
                if hasattr(arg, "effective_user") and arg.effective_user:
                    user_id = arg.effective_user.id
                if hasattr(arg, "effective_chat") and arg.effective_chat:
                    chat_id = arg.effective_chat.id
                if hasattr(arg, "chat") and arg.chat:
                    chat_id = arg.chat.id
                if hasattr(arg, "from_user") and arg.from_user:
                    user_id = arg.from_user.id

            start_time = api_logger.log_api_request(
                api_name=api_name, endpoint=endpoint, user_id=user_id, chat_id=chat_id
            )

            try:
                result = await func(*args, **kwargs)
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    response_data=result,
                    success=True,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                return result
            except Exception as e:
                api_logger.log_error(
                    api_name=api_name,
                    error=e,
                    context={"function": func.__name__},
                    user_id=user_id,
                    chat_id=chat_id,
                )
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    success=False,
                    error_message=str(e),
                    user_id=user_id,
                    chat_id=chat_id,
                )
                raise

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = api_logger.log_api_request(api_name=api_name, endpoint=endpoint)

            try:
                result = func(*args, **kwargs)
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    response_data=result,
                    success=True,
                )
                return result
            except Exception as e:
                api_logger.log_error(api_name=api_name, error=e, context={"function": func.__name__})
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    success=False,
                    error_message=str(e),
                )
                raise

        # Return асинхронную or синхронную обертку в зависимости от типа функции
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
