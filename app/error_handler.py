"""
Centralized error handling system for the bot.
Provides unified error handling, logging, and user feedback.
"""

import logging
import traceback
import asyncio
from typing import Optional, Dict, Any, Callable, Union
from functools import wraps

from app.exceptions import (
    GemaibotBaseException, DatabaseError, APIError, 
    NetworkError as AppNetworkError, SecurityError
)
from app.utils.api_logger import api_logger

class ErrorHandler:
    """
    Централизованный обработчик ошибок для всех компонентов бота
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.error_counts = {}
        self.max_errors_per_minute = 10
    
    def handle_telegram_error(self, func_name: str = "unknown"):
        """
        Декоратор для обработки ошибок Telegram API
        """
        def decorator(func: Callable):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    await self._handle_telegram_error(e, func_name, args, kwargs)
                    raise
            
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    self._handle_sync_error(e, func_name, args, kwargs)
                    raise
            
            # Возвращаем асинхронную или синхронную обертку
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper
        
        return decorator
    
    async def _handle_telegram_error(self, 
                                   error: Exception, 
                                   func_name: str, 
                                   args: tuple, 
                                   kwargs: dict):
        """Обрабатывает ошибки в асинхронных функциях"""
        try:
            # Извлекаем контекст из аргументов
            update = self._extract_update_from_args(args)
            user_id = self._extract_user_id_from_args(args)
            chat_id = self._extract_chat_id_from_args(args)
            
            # Логируем ошибку
            self._log_error(error, func_name, user_id, chat_id)
            
            # Обрабатываем специфичные ошибки Telegram
            error_msg = str(error).lower()
            
            if "network" in error_msg or "timeout" in error_msg:
                await self._handle_network_error(error, update, user_id, chat_id)
            elif "rate limit" in error_msg or "retry" in error_msg:
                await self._handle_rate_limit_error(error, update, user_id, chat_id)
            elif "bad request" in error_msg or "unauthorized" in error_msg or "forbidden" in error_msg:
                await self._handle_telegram_api_error(error, update, user_id, chat_id)
            elif "conflict" in error_msg:
                await self._handle_conflict_error(error, update, user_id, chat_id)
            else:
                await self._handle_generic_error(error, update, user_id, chat_id)
                
        except Exception as handler_error:
            # Логируем ошибку в обработчике ошибок
            self.logger.error(f"Error in error handler: {handler_error}")
            self.logger.error(f"Original error: {error}")
    
    def _handle_sync_error(self, 
                          error: Exception, 
                          func_name: str, 
                          args: tuple, 
                          kwargs: dict):
        """Обрабатывает ошибки в синхронных функциях"""
        try:
            # Извлекаем контекст из аргументов
            user_id = self._extract_user_id_from_args(args)
            chat_id = self._extract_chat_id_from_args(args)
            
            # Логируем ошибку
            self._log_error(error, func_name, user_id, chat_id)
            
            # Для синхронных функций просто логируем
            self.logger.error(f"Sync error in {func_name}: {error}")
            
        except Exception as handler_error:
            self.logger.error(f"Error in sync error handler: {handler_error}")
    
    def _extract_update_from_args(self, args: tuple):
        """Извлекает объект Update из аргументов функции"""
        for arg in args:
            if hasattr(arg, 'effective_user') or hasattr(arg, 'effective_chat'):
                return arg
        return None
    
    def _extract_user_id_from_args(self, args: tuple) -> Optional[int]:
        """Извлекает user_id из аргументов функции"""
        for arg in args:
            if hasattr(arg, 'effective_user') and arg.effective_user:
                return arg.effective_user.id
            elif hasattr(arg, 'from_user') and arg.from_user:
                return arg.from_user.id
        return None
    
    def _extract_chat_id_from_args(self, args: tuple) -> Optional[int]:
        """Извлекает chat_id из аргументов функции"""
        for arg in args:
            if hasattr(arg, 'effective_chat') and arg.effective_chat:
                return arg.effective_chat.id
            elif hasattr(arg, 'chat') and arg.chat:
                return arg.chat.id
        return None
    
    def _log_error(self, 
                   error: Exception, 
                   func_name: str, 
                   user_id: Optional[int] = None,
                   chat_id: Optional[int] = None):
        """Логирует ошибку с контекстом"""
        error_context = {
            "function": func_name,
            "user_id": user_id,
            "chat_id": chat_id,
            "error_type": type(error).__name__,
            "error_message": str(error)
        }
        
        # Логируем в основной лог
        self.logger.error(
            f"Error in {func_name}: {error} | User: {user_id} | Chat: {chat_id}",
            extra=error_context
        )
        
        # Логируем в API логгер если это API ошибка
        error_msg = str(error).lower()
        if any(keyword in error_msg for keyword in ["network", "timeout", "bad request", "unauthorized", "forbidden"]):
            api_logger.log_error(
                api_name="telegram",
                error=error,
                context=error_context,
                user_id=user_id,
                chat_id=chat_id
            )
    
    async def _handle_network_error(self, 
                                  error: Exception, 
                                  update,
                                  user_id: Optional[int],
                                  chat_id: Optional[int]):
        """Обрабатывает сетевые ошибки"""
        if update and hasattr(update, 'message') and update.message:
            try:
                await update.message.reply_text(
                    "⚠️ Произошла сетевая ошибка. Попробуйте позже."
                )
            except Exception as reply_error:
                self.logger.error(f"Could not send network error message: {reply_error}")
        
        # Логируем для мониторинга
        self.logger.warning(f"Network error for user {user_id} in chat {chat_id}: {error}")
    
    async def _handle_rate_limit_error(self, 
                                     error: Exception, 
                                     update,
                                     user_id: Optional[int],
                                     chat_id: Optional[int]):
        """Обрабатывает ошибки превышения лимитов"""
        if update and hasattr(update, 'message') and update.message:
            try:
                retry_after = getattr(error, 'retry_after', 60)
                await update.message.reply_text(
                    f"⏳ Слишком много запросов. Попробуйте через {retry_after} секунд."
                )
            except Exception as reply_error:
                self.logger.error(f"Could not send rate limit message: {reply_error}")
        
        # Логируем для мониторинга
        self.logger.warning(f"Rate limit exceeded for user {user_id} in chat {chat_id}: {error}")
    
    async def _handle_telegram_api_error(self, 
                                       error: Exception, 
                                       update,
                                       user_id: Optional[int],
                                       chat_id: Optional[int]):
        """Обрабатывает ошибки Telegram API"""
        if update and hasattr(update, 'message') and update.message:
            try:
                error_msg = str(error).lower()
                if "bad request" in error_msg:
                    await update.message.reply_text(
                        "❌ Некорректный запрос. Проверьте параметры."
                    )
                elif "unauthorized" in error_msg:
                    await update.message.reply_text(
                        "🔐 Ошибка авторизации. Обратитесь к администратору."
                    )
                elif "forbidden" in error_msg:
                    await update.message.reply_text(
                        "🚫 Доступ запрещен. Проверьте права бота."
                    )
                else:
                    await update.message.reply_text(
                        "❌ Произошла ошибка API. Попробуйте позже."
                    )
            except Exception as reply_error:
                self.logger.error(f"Could not send API error message: {reply_error}")
        
        # Логируем для мониторинга
        self.logger.error(f"Telegram API error for user {user_id} in chat {chat_id}: {error}")
    
    async def _handle_conflict_error(self, 
                                   error: Exception, 
                                   update,
                                   user_id: Optional[int],
                                   chat_id: Optional[int]):
        """Обрабатывает конфликтные ошибки"""
        if update and hasattr(update, 'message') and update.message:
            try:
                await update.message.reply_text(
                    "⚠️ Произошел конфликт. Попробуйте еще раз."
                )
            except Exception as reply_error:
                self.logger.error(f"Could not send conflict error message: {reply_error}")
        
        # Логируем для мониторинга
        self.logger.warning(f"Conflict error for user {user_id} in chat {chat_id}: {error}")
    
    async def _handle_generic_error(self, 
                                  error: Exception, 
                                  update,
                                  user_id: Optional[int],
                                  chat_id: Optional[int]):
        """Обрабатывает общие ошибки"""
        if update and hasattr(update, 'message') and update.message:
            try:
                await update.message.reply_text(
                    "❌ Произошла непредвиденная ошибка. Попробуйте позже."
                )
            except Exception as reply_error:
                self.logger.error(f"Could not send generic error message: {reply_error}")
        
        # Логируем для мониторинга
        self.logger.error(f"Generic error for user {user_id} in chat {chat_id}: {error}")
    
    def handle_database_error(self, func_name: str = "unknown"):
        """
        Декоратор для обработки ошибок базы данных
        """
        def decorator(func: Callable):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    await self._handle_database_error(e, func_name, args, kwargs)
                    raise
            
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    self._handle_sync_database_error(e, func_name, args, kwargs)
                    raise
            
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper
        
        return decorator
    
    async def _handle_database_error(self, 
                                   error: Exception, 
                                   func_name: str, 
                                   args: tuple, 
                                   kwargs: dict):
        """Обрабатывает ошибки базы данных"""
        try:
            user_id = self._extract_user_id_from_args(args)
            chat_id = self._extract_chat_id_from_args(args)
            
            # Логируем ошибку
            self._log_error(error, func_name, user_id, chat_id)
            
            # Определяем тип ошибки базы данных
            error_msg = str(error).lower()
            
            if "rate limit" in error_msg or "quota" in error_msg:
                self.logger.error(f"Database rate limit exceeded in {func_name}")
            elif "connection" in error_msg or "timeout" in error_msg:
                self.logger.error(f"Database connection issue in {func_name}")
            else:
                self.logger.error(f"Database error in {func_name}: {error}")
                
        except Exception as handler_error:
            self.logger.error(f"Error in database error handler: {handler_error}")
    
    def _handle_sync_database_error(self, 
                                  error: Exception, 
                                  func_name: str, 
                                  args: tuple, 
                                  kwargs: dict):
        """Обрабатывает синхронные ошибки базы данных"""
        try:
            user_id = self._extract_user_id_from_args(args)
            chat_id = self._extract_chat_id_from_args(args)
            
            self._log_error(error, func_name, user_id, chat_id)
            self.logger.error(f"Sync database error in {func_name}: {error}")
            
        except Exception as handler_error:
            self.logger.error(f"Error in sync database error handler: {handler_error}")
    
    def handle_api_error(self, api_name: str, func_name: str = "unknown"):
        """
        Декоратор для обработки ошибок внешних API
        """
        def decorator(func: Callable):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    await self._handle_api_error(e, api_name, func_name, args, kwargs)
                    raise
            
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    self._handle_sync_api_error(e, api_name, func_name, args, kwargs)
                    raise
            
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper
        
        return decorator
    
    async def _handle_api_error(self, 
                              error: Exception, 
                              api_name: str,
                              func_name: str, 
                              args: tuple, 
                              kwargs: dict):
        """Обрабатывает ошибки внешних API"""
        try:
            user_id = self._extract_user_id_from_args(args)
            chat_id = self._extract_chat_id_from_args(args)
            
            # Логируем ошибку
            self._log_error(error, func_name, user_id, chat_id)
            
            # Логируем в API логгер
            api_logger.log_error(
                api_name=api_name,
                error=error,
                context={"function": func_name},
                user_id=user_id,
                chat_id=chat_id
            )
            
            # Определяем тип ошибки API
            error_msg = str(error).lower()
            
            if "quota" in error_msg or "limit" in error_msg:
                self.logger.error(f"API quota exceeded for {api_name} in {func_name}")
            elif "timeout" in error_msg:
                self.logger.error(f"API timeout for {api_name} in {func_name}")
            else:
                self.logger.error(f"API error for {api_name} in {func_name}: {error}")
                
        except Exception as handler_error:
            self.logger.error(f"Error in API error handler: {handler_error}")
    
    def _handle_sync_api_error(self, 
                             error: Exception, 
                             api_name: str,
                             func_name: str, 
                             args: tuple, 
                             kwargs: dict):
        """Обрабатывает синхронные ошибки API"""
        try:
            user_id = self._extract_user_id_from_args(args)
            chat_id = self._extract_chat_id_from_args(args)
            
            self._log_error(error, func_name, user_id, chat_id)
            
            api_logger.log_error(
                api_name=api_name,
                error=error,
                context={"function": func_name},
                user_id=user_id,
                chat_id=chat_id
            )
            
            self.logger.error(f"Sync API error for {api_name} in {func_name}: {error}")
            
        except Exception as handler_error:
            self.logger.error(f"Error in sync API error handler: {handler_error}")

# Глобальный экземпляр обработчика ошибок
error_handler = ErrorHandler()

# Удобные функции-декораторы
def handle_telegram_error(func_name: str = "unknown"):
    """Декоратор для обработки ошибок Telegram"""
    return error_handler.handle_telegram_error(func_name)

def handle_database_error(func_name: str = "unknown"):
    """Декоратор для обработки ошибок базы данных"""
    return error_handler.handle_database_error(func_name)

def handle_api_error(api_name: str, func_name: str = "unknown"):
    """Декоратор для обработки ошибок API"""
    return error_handler.handle_api_error(api_name, func_name)

# Функция safe_execute для обратной совместимости
async def safe_execute(func: Callable, *args, **kwargs) -> Any:
    """
    Безопасно выполняет функцию с обработкой ошибок.
    Добавлена для обратной совместимости.
    """
    try:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)
    except Exception as e:
        logging.error(f"Error in safe_execute: {e}")
        return f"❌ Произошла ошибка: {str(e)}"
