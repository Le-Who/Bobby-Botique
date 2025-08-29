"""
Centralized error handling system for the bot.
Provides consistent error handling, logging, and user feedback across all components.
"""

import logging
import traceback
import asyncio
from typing import Optional, Dict, Any, Callable, Type, Union
from functools import wraps
from telegram import Update, Message
from telegram.ext import ContextTypes

from app.exceptions import (
    GemaibotBaseException, DatabaseError, APIError, NetworkError,
    SecurityError, convert_to_typed_exception
)
from app.utils.api_logger import api_logger

class ErrorHandler:
    """
    Централизованный обработчик ошибок для всего бота.
    Обеспечивает единообразную обработку, логирование и уведомления пользователей.
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.error_counts = {}
        self.max_errors_per_minute = 10
    
    async def handle_error(self, 
                          error: Exception, 
                          context: str = "",
                          user_id: Optional[int] = None,
                          chat_id: Optional[int] = None,
                          update: Optional[Update] = None,
                          context_types: Optional[ContextTypes.DEFAULT_TYPE] = None,
                          **kwargs) -> str:
        """
        Основной метод обработки ошибок.
        
        Args:
            error: Исключение для обработки
            context: Контекст, в котором произошла ошибка
            user_id: ID пользователя
            chat_id: ID чата
            update: Объект Update от Telegram
            context_types: Контекст Telegram
            **kwargs: Дополнительные параметры
            
        Returns:
            Сообщение об ошибке для пользователя
        """
        try:
            # Конвертируем в типизированное исключение
            typed_error = convert_to_typed_exception(error, context)
            
            # Логируем ошибку
            await self._log_error(typed_error, context, user_id, chat_id, **kwargs)
            
            # Обновляем счетчики ошибок
            self._update_error_count(context)
            
            # Получаем сообщение для пользователя
            user_message = self._get_user_friendly_message(typed_error, context)
            
            # Отправляем уведомление пользователю если возможно
            if update and context_types:
                await self._notify_user(update, context_types, user_message, typed_error)
            
            return user_message
            
        except Exception as e:
            # Если произошла ошибка в обработчике ошибок
            self.logger.error(f"Error in error handler: {e}")
            return "❌ Произошла неожиданная ошибка. Попробуйте позже."
    
    async def _log_error(self, 
                         error: GemaibotBaseException, 
                         context: str,
                         user_id: Optional[int] = None,
                         chat_id: Optional[int] = None,
                         **kwargs) -> None:
        """Логирует ошибку с полным контекстом"""
        error_data = {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context,
            "user_id": user_id,
            "chat_id": chat_id,
            "details": error.details,
            **kwargs
        }
        
        # Логируем в основной лог
        self.logger.error(
            f"Error in {context}: {error.message}",
            extra=error_data,
            exc_info=True
        )
        
        # Логируем в API логгер если это API ошибка
        if isinstance(error, APIError):
            api_logger.log_error(
                api_name=context,
                error=error,
                context=error_data,
                user_id=user_id,
                chat_id=chat_id
            )
    
    def _update_error_count(self, context: str) -> None:
        """Обновляет счетчик ошибок для контекста"""
        current_time = asyncio.get_event_loop().time()
        
        if context not in self.error_counts:
            self.error_counts[context] = []
        
        # Удаляем ошибки старше 1 минуты
        self.error_counts[context] = [
            time for time in self.error_counts[context] 
            if current_time - time < 60
        ]
        
        # Добавляем текущую ошибку
        self.error_counts[context].append(current_time)
        
        # Логируем предупреждение если слишком много ошибок
        if len(self.error_counts[context]) >= self.max_errors_per_minute:
            self.logger.warning(
                f"High error rate in {context}: {len(self.error_counts[context])} errors per minute"
            )
    
    def _get_user_friendly_message(self, error: GemaibotBaseException, context: str) -> str:
        """Возвращает понятное пользователю сообщение об ошибке"""
        error_type = type(error).__name__
        
        # Базовые сообщения для разных типов ошибок
        base_messages = {
            "DatabaseError": "❌ Ошибка базы данных",
            "DatabaseConnectionError": "❌ Ошибка подключения к базе данных",
            "DatabaseQueryError": "❌ Ошибка выполнения запроса",
            "DatabaseRateLimitError": "⏰ Превышен лимит запросов к базе данных",
            
            "APIError": "❌ Ошибка внешнего API",
            "APIQuotaExceededError": "🚫 Достигнут лимит запросов к API",
            "APITimeoutError": "⏰ Превышено время ожидания ответа от API",
            
            "NetworkError": "🌐 Ошибка сети",
            "ConnectionTimeoutError": "⏰ Превышено время подключения",
            "ConnectionRefusedError": "🚫 Соединение отклонено",
            
            "SecurityError": "🔒 Ошибка безопасности",
            "AuthenticationError": "🔐 Ошибка аутентификации",
            "AuthorizationError": "🚫 Недостаточно прав",
            
            "ValidationError": "⚠️ Ошибка валидации данных",
            "RateLimitError": "⏰ Превышен лимит запросов",
            "TimeoutError": "⏰ Превышено время выполнения"
        }
        
        # Специальная обработка для 503 ошибок и NetworkError
        if error.details and "original_error" in error.details:
            original_error = str(error.details["original_error"]).lower()
            
            # 503 ошибки Gemini API
            if "503" in original_error and ("unavailable" in original_error or "overloaded" in original_error):
                return "🔄 Сервер Gemini временно перегружен. Попробуйте через несколько минут."
            
            # NetworkError httpx.ReadError
            if "httpx.readerror" in original_error or "networkerror" in original_error:
                return "🌐 Проблемы с сетевым соединением. Попробуйте позже."
        
        # Получаем базовое сообщение
        base_message = base_messages.get(error_type, "❌ Произошла ошибка")
        
        # Добавляем детали если они есть
        if error.details and "original_error" in error.details:
            original_error = error.details["original_error"]
            if "quota" in str(original_error).lower():
                return "🚫 Достигнут лимит запросов. Попробуйте позже."
            elif "timeout" in str(original_error).lower():
                return "⏰ Превышено время ожидания. Попробуйте позже."
            elif "connection" in str(original_error).lower():
                return "🌐 Проблемы с подключением. Попробуйте позже."
        
        return base_message
    
    async def _notify_user(self, 
                           update: Update, 
                           context_types: ContextTypes.DEFAULT_TYPE,
                           message: str,
                           error: GemaibotBaseException) -> None:
        """Отправляет уведомление пользователю об ошибке"""
        try:
            if update.message:
                await update.message.reply_text(message)
            elif update.callback_query:
                await update.callback_query.answer(message, show_alert=True)
        except Exception as e:
            self.logger.error(f"Failed to notify user about error: {e}")
    
    def handle_sync(self, context: str = ""):
        """Декоратор для синхронных функций"""
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    # Для синхронных функций возвращаем сообщение об ошибке
                    return self._get_user_friendly_message(
                        convert_to_typed_exception(e, context), 
                        context
                    )
            return wrapper
        return decorator
    
    def handle_async(self, context: str = ""):
        """Декоратор для асинхронных функций"""
        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # Для асинхронных функций возвращаем сообщение об ошибке
                    return self._get_user_friendly_message(
                        convert_to_typed_exception(e, context), 
                        context
                    )
            return wrapper
        return decorator
    
    def handle_telegram_update(self, context: str = ""):
        """Декоратор для обработчиков Telegram обновлений"""
        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(update: Update, context_types: ContextTypes.DEFAULT_TYPE):
                try:
                    return await func(update, context_types)
                except Exception as e:
                    # Извлекаем информацию о пользователе
                    user_id = None
                    chat_id = None
                    
                    if update.effective_user:
                        user_id = update.effective_user.id
                    if update.effective_chat:
                        chat_id = update.effective_chat.id
                    
                    # Обрабатываем ошибку
                    return await self.handle_error(
                        error=e,
                        context=context,
                        user_id=user_id,
                        chat_id=chat_id,
                        update=update,
                        context_types=context_types
                    )
            return wrapper
        return decorator

# Глобальный экземпляр обработчика ошибок
error_handler = ErrorHandler()

# Удобные функции для быстрого использования
def handle_error_sync(context: str = ""):
    """Быстрый декоратор для синхронных функций"""
    return error_handler.handle_sync(context)

def handle_error_async(context: str = ""):
    """Быстрый декоратор для асинхронных функций"""
    return error_handler.handle_async(context)

def handle_telegram_error(context: str = ""):
    """Быстрый декоратор для обработчиков Telegram"""
    return error_handler.handle_telegram_update(context)

async def safe_execute(func: Callable, 
                      *args, 
                      context: str = "",
                      user_id: Optional[int] = None,
                      chat_id: Optional[int] = None,
                      **kwargs) -> Any:
    """
    Безопасно выполняет функцию с обработкой ошибок.
    
    Args:
        func: Функция для выполнения
        *args: Аргументы функции
        context: Контекст выполнения
        user_id: ID пользователя
        chat_id: ID чата
        **kwargs: Дополнительные аргументы
        
    Returns:
        Результат выполнения функции или сообщение об ошибке
    """
    try:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)
    except Exception as e:
        return await error_handler.handle_error(
            error=e,
            context=context,
            user_id=user_id,
            chat_id=chat_id
        )
