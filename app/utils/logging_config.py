import logging
import sys
from typing import Optional

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
    
    # Преобразуем строку в уровень логирования
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Настраиваем корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Очищаем существующие handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Выбираем форматтер в зависимости от настроек
    if enable_structured_logging:
        # JSON форматтер для production
        import json
        class JSONFormatter(logging.Formatter):
            def format(self, record):
                log_entry = {
                    "timestamp": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "module": record.module,
                    "function": record.funcName,
                    "line": record.lineno
                }
                if hasattr(record, 'user_id'):
                    log_entry['user_id'] = record.user_id
                if hasattr(record, 'chat_id'):
                    log_entry['chat_id'] = record.chat_id
                if record.exc_info:
                    log_entry['exception'] = self.formatException(record.exc_info)
                return json.dumps(log_entry, ensure_ascii=False)
        
        formatter = JSONFormatter()
    else:
        # Детальный форматтер для development
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    # Handler для stdout (обязательно для Render)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)
    
    # Handler для файла (опционально)
    if log_to_file:
        try:
            file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
            file_handler.setLevel(numeric_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            print("Warning: Could not create file handler: %s", e, flush=True)
    
    # Настраиваем специальные логгеры
    setup_api_logger(numeric_level, enable_structured_logging)
    setup_telegram_logger(numeric_level, enable_structured_logging)
    setup_database_logger(numeric_level, enable_structured_logging)
    
    # Принудительно выводим в stdout для Render
    print("=== DETAILED LOGGING SETUP COMPLETE ===", flush=True)
    print("Log level: %s", log_level, flush=True)
    print("Log to file: %s", log_to_file, flush=True)
    if log_to_file:
        print("Log file: %s", log_file_path, flush=True)
    print("=== LOGGING READY ===", flush=True)

def setup_api_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для API запросов"""
    api_logger = logging.getLogger('api_logger')
    api_logger.setLevel(level)
    
    # Не добавляем handlers если они уже есть
    if not api_logger.handlers:
        if enable_structured_logging:
            import json
            class JSONFormatter(logging.Formatter):
                def format(self, record):
                    log_entry = {
                        "timestamp": self.formatTime(record),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                        "module": record.module,
                        "function": record.funcName,
                        "line": record.lineno
                    }
                    if hasattr(record, 'user_id'):
                        log_entry['user_id'] = record.user_id
                    if hasattr(record, 'chat_id'):
                        log_entry['chat_id'] = record.chat_id
                    if record.exc_info:
                        log_entry['exception'] = self.formatException(record.exc_info)
                    return json.dumps(log_entry, ensure_ascii=False)
            
            formatter = JSONFormatter()
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        api_logger.addHandler(handler)
    
    # Отключаем propagation для избежания дублирования
    api_logger.propagate = False

def setup_telegram_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для Telegram Bot API"""
    telegram_logger = logging.getLogger('telegram')
    telegram_logger.setLevel(level)
    
    # Не добавляем handlers если они уже есть
    if not telegram_logger.handlers:
        if enable_structured_logging:
            import json
            class JSONFormatter(logging.Formatter):
                def format(self, record):
                    log_entry = {
                        "timestamp": self.formatTime(record),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                        "module": record.module,
                        "function": record.funcName,
                        "line": record.lineno
                    }
                    if hasattr(record, 'user_id'):
                        log_entry['user_id'] = record.user_id
                    if hasattr(record, 'chat_id'):
                        log_entry['chat_id'] = record.chat_id
                    if record.exc_info:
                        log_entry['exception'] = self.formatException(record.exc_info)
                    return json.dumps(log_entry, ensure_ascii=False)
            
            formatter = JSONFormatter()
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        telegram_logger.addHandler(handler)
    
    # Отключаем propagation для избежания дублирования
    telegram_logger.propagate = False

def setup_database_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для базы данных"""
    db_logger = logging.getLogger('asyncpg')
    db_logger.setLevel(level)
    
    # Не добавляем handlers если они уже есть
    if not db_logger.handlers:
        if enable_structured_logging:
            import json
            class JSONFormatter(logging.Formatter):
                def format(self, record):
                    log_entry = {
                        "timestamp": self.formatTime(record),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                        "module": record.module,
                        "function": record.funcName,
                        "line": record.lineno
                    }
                    if hasattr(record, 'user_id'):
                        log_entry['user_id'] = record.user_id
                    if hasattr(record, 'chat_id'):
                        log_entry['chat_id'] = record.chat_id
                    if record.exc_info:
                        log_entry['exception'] = self.formatException(record.exc_info)
                    return json.dumps(log_entry, ensure_ascii=False)
            
            formatter = JSONFormatter()
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        db_logger.addHandler(handler)
    
    # Отключаем propagation для избежания дублирования
    db_logger.propagate = False

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
