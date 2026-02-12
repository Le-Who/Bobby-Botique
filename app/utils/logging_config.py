"""
Centralized logging configuration for GemAI Bot.

Provides:
- setup_detailed_logging() for initial configuration
- JSONFormatter for structured production logging
- get_logger() helper for module-specific loggers
- log_with_context() for adding user/chat context to logs
"""
import logging
import sys
import json
from typing import Optional, Any

from app.request_context import get_request_id


# =============================================================================
# FORMATTERS
# =============================================================================

class JSONFormatter(logging.Formatter):
    """
    JSON formatter for structured logging in production.
    
    Automatically includes:
    - timestamp, level, logger, message
    - module, function, line number
    - user_id, chat_id if set on record
    - exception info if present
    """
    
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        # Add context fields if present
        if hasattr(record, 'user_id'):
            log_entry['user_id'] = record.user_id
        if hasattr(record, 'chat_id'):
            log_entry['chat_id'] = record.chat_id
        if hasattr(record, 'extra_context'):
            log_entry['context'] = record.extra_context
        if hasattr(record, 'request_id'):
            log_entry['request_id'] = record.request_id
            
        # Add exception info
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)
            
        return json.dumps(log_entry, ensure_ascii=False)




class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        request_id = get_request_id()
        if not hasattr(record, 'request_id'):
            record.request_id = request_id or '-'
        return True
# Default formatter for development
DEFAULT_FORMATTER = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [request_id=%(request_id)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


# =============================================================================
# LOGGER HELPERS
# =============================================================================

def get_logger(name: str) -> logging.Logger:
    """
    Get a logger by name with standard configuration.
    
    Usage:
        logger = get_logger(__name__)
        logger.info("Processing request", extra={"user_id": 123})
    """
    return logging.getLogger(name)


def log_with_context(
    logger: logging.Logger,
    level: int,
    message: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    **extra: Any
) -> None:
    """
    Log a message with user/chat context attached.
    
    Args:
        logger: Logger instance
        level: Logging level (logging.INFO, logging.ERROR, etc.)
        message: Log message
        user_id: Optional user ID to attach
        chat_id: Optional chat ID to attach
        **extra: Additional context to include
        
    Usage:
        log_with_context(logger, logging.INFO, "User action", user_id=123)
    """
    extra_dict = {}
    if user_id is not None:
        extra_dict['user_id'] = user_id
    if chat_id is not None:
        extra_dict['chat_id'] = chat_id
    if extra:
        extra_dict['extra_context'] = extra
    
    logger.log(level, message, extra=extra_dict)


# =============================================================================
# SETUP FUNCTIONS
# =============================================================================

def _get_formatter(enable_structured_logging: bool) -> logging.Formatter:
    """Get appropriate formatter based on configuration."""
    return JSONFormatter() if enable_structured_logging else DEFAULT_FORMATTER


def _setup_logger(
    logger_name: str,
    level: int,
    enable_structured_logging: bool
) -> None:
    """
    Configure a named logger with standard settings.
    
    Args:
        logger_name: Name of the logger to configure
        level: Logging level
        enable_structured_logging: Whether to use JSON format
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    
    # Don't add handlers if they already exist
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(_get_formatter(enable_structured_logging))
        logger.addHandler(handler)
    
    # Disable propagation to avoid duplicate logs
    logger.propagate = False


def setup_detailed_logging(
    log_level: str = "INFO",
    log_to_file: bool = False,
    log_file_path: str = "/tmp/bot_detailed.log",
    enable_structured_logging: bool = False
) -> None:
    """
    Настраивает детальное логирование для всех компонентов бота
    
    Args:
        log_level: Уровень логирования (INFO, WARNING, ERROR, CRITICAL)
        log_to_file: Логировать ли в файл
        log_file_path: Путь к файлу логов
        enable_structured_logging: Включить JSON логирование для production
    """
    # Convert string to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    formatter = _get_formatter(enable_structured_logging)
    
    # Handler for stdout (required for Render)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.addFilter(RequestContextFilter())
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)
    
    # File handler (optional)
    if log_to_file:
        try:
            file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
            file_handler.setLevel(numeric_level)
            file_handler.addFilter(RequestContextFilter())
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Could not create file handler: {e}", flush=True)
    
    # Configure specialized loggers
    for logger_name in ['api_logger', 'telegram', 'asyncpg']:
        _setup_logger(logger_name, numeric_level, enable_structured_logging)
    
    # Status output
    print("=== DETAILED LOGGING SETUP COMPLETE ===", flush=True)
    print(f"Log level: {log_level}", flush=True)
    print(f"Log to file: {log_to_file}", flush=True)
    if log_to_file:
        print(f"Log file: {log_file_path}", flush=True)
    print("=== LOGGING READY ===", flush=True)


# Legacy compatibility functions
def setup_api_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для API запросов"""
    _setup_logger('api_logger', level, enable_structured_logging)


def setup_telegram_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для Telegram Bot API"""
    _setup_logger('telegram', level, enable_structured_logging)


def setup_database_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для базы данных"""
    _setup_logger('asyncpg', level, enable_structured_logging)


def log_api_summary() -> None:
    """Выводит краткую сводку по API запросам"""
    print("=== API LOGGING SUMMARY ===", flush=True)
    print("✅ Gemini API - детальное логирование запросов и ответов", flush=True)
    print("✅ Tavily API - детальное логирование поисковых запросов", flush=True)
    print("✅ Telegram Bot API - детальное логирование обработки сообщений", flush=True)
    print("✅ Все API запросы логируются с временем выполнения", flush=True)
    print("✅ Ошибки API логируются с полным стектрейсом", flush=True)
    print("✅ Чувствительные данные (API ключи) автоматически скрываются", flush=True)
    print("=== SUMMARY COMPLETE ===", flush=True)

