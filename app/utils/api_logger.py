import logging
import time
import json
import traceback
from typing import Dict, Any, Optional, Callable
from functools import wraps
from datetime import datetime
import asyncio

class APILogger:
    """
    Детальное логирование для всех API запросов (Telegram, Gemini, Tavily)
    """
    
    def __init__(self):
        self.logger = logging.getLogger('api_logger')
        self.logger.setLevel(logging.INFO)
        
        # Создаем форматтер для детального логирования
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Добавляем handler если его нет
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
    
    def log_api_request(self, 
                       api_name: str, 
                       endpoint: str, 
                       method: str = "GET",
                       request_data: Optional[Dict[str, Any]] = None,
                       user_id: Optional[int] = None,
                       chat_id: Optional[int] = None):
        """Логирует начало API запроса"""
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "endpoint": endpoint,
            "method": method,
            "user_id": user_id,
            "chat_id": chat_id,
            "request_data": self._sanitize_data(request_data),
            "status": "STARTED"
        }
        
        self.logger.info(f"🚀 API REQUEST STARTED: {json.dumps(log_data, ensure_ascii=False)}")
        return time.time()
    
    def log_api_response(self, 
                        api_name: str, 
                        endpoint: str,
                        start_time: float,
                        response_data: Optional[Dict[str, Any]] = None,
                        status_code: Optional[int] = None,
                        success: bool = True,
                        error_message: Optional[str] = None,
                        user_id: Optional[int] = None,
                        chat_id: Optional[int] = None):
        """Логирует завершение API запроса"""
        duration = time.time() - start_time
        
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "endpoint": endpoint,
            "duration_ms": round(duration * 1000, 2),
            "status_code": status_code,
            "success": success,
            "user_id": user_id,
            "chat_id": chat_id,
            "response_summary": self._summarize_response(response_data),
            "error_message": error_message,
            "status": "COMPLETED"
        }
        
        if success:
            self.logger.info(f"✅ API REQUEST COMPLETED: {json.dumps(log_data, ensure_ascii=False)}")
        else:
            self.logger.error(f"❌ API REQUEST FAILED: {json.dumps(log_data, ensure_ascii=False)}")
        
        return duration
    
    def log_gemini_request(self, 
                          model: str, 
                          prompt_length: int, 
                          has_images: bool = False,
                          user_id: Optional[int] = None,
                          chat_id: Optional[int] = None):
        """Специальное логирование для Gemini API"""
        start_time = time.time()
        
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "gemini",
            "model": model,
            "prompt_length": prompt_length,
            "has_images": has_images,
            "user_id": user_id,
            "chat_id": chat_id,
            "status": "STARTED"
        }
        
        self.logger.info(f"🤖 GEMINI REQUEST STARTED: {json.dumps(log_data, ensure_ascii=False)}")
        return start_time
    
    def log_gemini_response(self, 
                           start_time: float,
                           model: str,
                           response_length: int,
                           token_count: Optional[int] = None,
                           success: bool = True,
                           error_message: Optional[str] = None,
                           user_id: Optional[int] = None,
                           chat_id: Optional[int] = None):
        """Логирует ответ Gemini API"""
        duration = time.time() - start_time
        
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "gemini",
            "model": model,
            "duration_ms": round(duration * 1000, 2),
            "response_length": response_length,
            "token_count": token_count,
            "success": success,
            "user_id": user_id,
            "chat_id": chat_id,
            "error_message": error_message,
            "status": "COMPLETED"
        }
        
        if success:
            self.logger.info(f"✅ GEMINI RESPONSE COMPLETED: {json.dumps(log_data, ensure_ascii=False)}")
        else:
            self.logger.error(f"❌ GEMINI RESPONSE FAILED: {json.dumps(log_data, ensure_ascii=False)}")
        
        return duration
    
    def log_tavily_request(self, 
                          query: str, 
                          search_type: str,
                          user_id: Optional[int] = None,
                          chat_id: Optional[int] = None):
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
            "status": "STARTED"
        }
        
        self.logger.info(f"🔍 TAVILY REQUEST STARTED: {json.dumps(log_data, ensure_ascii=False)}")
        return start_time
    
    def log_tavily_response(self, 
                           start_time: float,
                           search_type: str,
                           results_count: int,
                           success: bool = True,
                           error_message: Optional[str] = None,
                           user_id: Optional[int] = None,
                           chat_id: Optional[int] = None):
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
            "status": "COMPLETED"
        }
        
        if success:
            self.logger.info(f"✅ TAVILY RESPONSE COMPLETED: {json.dumps(log_data, ensure_ascii=False)}")
        else:
            self.logger.error(f"❌ TAVILY RESPONSE FAILED: {json.dumps(log_data, ensure_ascii=False)}")
        
        return duration
    
    def log_telegram_request(self, 
                            method: str,
                            chat_id: Optional[int] = None,
                            user_id: Optional[int] = None,
                            message_type: Optional[str] = None):
        """Специальное логирование для Telegram Bot API"""
        start_time = time.time()
        
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "api": "telegram",
            "method": method,
            "chat_id": chat_id,
            "user_id": user_id,
            "message_type": message_type,
            "status": "STARTED"
        }
        
        self.logger.info(f"📱 TELEGRAM REQUEST STARTED: {json.dumps(log_data, ensure_ascii=False)}")
        return start_time
    
    def log_telegram_response(self, 
                             start_time: float,
                             method: str,
                             success: bool = True,
                             error_message: Optional[str] = None,
                             chat_id: Optional[int] = None,
                             user_id: Optional[int] = None):
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
            "status": "COMPLETED"
        }
        
        if success:
            self.logger.info(f"✅ TELEGRAM RESPONSE COMPLETED: {json.dumps(log_data, ensure_ascii=False)}")
        else:
            self.logger.error(f"❌ TELEGRAM RESPONSE FAILED: {json.dumps(log_data, ensure_ascii=False)}")
        
        return duration
    
    def log_error(self, 
                  api_name: str, 
                  error: Exception, 
                  context: Optional[Dict[str, Any]] = None,
                  user_id: Optional[int] = None,
                  chat_id: Optional[int] = None):
        """Логирует ошибки API с полным стектрейсом"""
        error_data = {
            "timestamp": datetime.now().isoformat(),
            "api": api_name,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc(),
            "context": context,
            "user_id": user_id,
            "chat_id": chat_id,
            "status": "ERROR"
        }
        
        self.logger.error(f"💥 API ERROR: {json.dumps(error_data, ensure_ascii=False)}")
    
    def _sanitize_data(self, data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Очищает чувствительные данные из логов"""
        if not data:
            return None
        
        sanitized = data.copy()
        sensitive_keys = ['api_key', 'token', 'password', 'secret']
        
        for key in sensitive_keys:
            if key in sanitized:
                if isinstance(sanitized[key], str) and len(sanitized[key]) > 8:
                    sanitized[key] = sanitized[key][:4] + "..." + sanitized[key][-4:]
                else:
                    sanitized[key] = "***"
        
        return sanitized
    
    def _summarize_response(self, response_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Создает краткое описание ответа"""
        if not response_data:
            return None
        
        summary = {}
        
        if isinstance(response_data, dict):
            # Подсчитываем размер ответа
            if 'text' in response_data:
                summary['text_length'] = len(str(response_data['text']))
            if 'results' in response_data:
                summary['results_count'] = len(response_data['results'])
            if 'content' in response_data:
                summary['content_length'] = len(str(response_data['content']))
        
        return summary

# Глобальный экземпляр логгера
api_logger = APILogger()

def log_api_call(api_name: str, endpoint: str = ""):
    """Декоратор для логирования API вызовов"""
    def decorator(func: Callable):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Извлекаем user_id и chat_id из аргументов если возможно
            user_id = None
            chat_id = None
            
            # Ищем объекты Update или Message в аргументах
            for arg in args:
                if hasattr(arg, 'effective_user') and arg.effective_user:
                    user_id = arg.effective_user.id
                if hasattr(arg, 'effective_chat') and arg.effective_chat:
                    chat_id = arg.effective_chat.id
                if hasattr(arg, 'chat') and arg.chat:
                    chat_id = arg.chat.id
                if hasattr(arg, 'from_user') and arg.from_user:
                    user_id = arg.from_user.id
            
            start_time = api_logger.log_api_request(
                api_name=api_name,
                endpoint=endpoint,
                user_id=user_id,
                chat_id=chat_id
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
                    chat_id=chat_id
                )
                return result
            except Exception as e:
                api_logger.log_error(
                    api_name=api_name,
                    error=e,
                    context={"function": func.__name__},
                    user_id=user_id,
                    chat_id=chat_id
                )
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    success=False,
                    error_message=str(e),
                    user_id=user_id,
                    chat_id=chat_id
                )
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = api_logger.log_api_request(
                api_name=api_name,
                endpoint=endpoint
            )
            
            try:
                result = func(*args, **kwargs)
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    response_data=result,
                    success=True
                )
                return result
            except Exception as e:
                api_logger.log_error(
                    api_name=api_name,
                    error=e,
                    context={"function": func.__name__}
                )
                api_logger.log_api_response(
                    api_name=api_name,
                    endpoint=endpoint,
                    start_time=start_time,
                    success=False,
                    error_message=str(e)
                )
                raise
        
        # Возвращаем асинхронную или синхронную обертку в зависимости от типа функции
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator
