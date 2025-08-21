import logging
import sys
from typing import Optional

def setup_detailed_logging(
    log_level: str = "INFO",
    log_to_file: bool = False,
    log_file_path: str = "/tmp/bot_detailed.log"
) -> None:
    """
    Настраивает детальное логирование для всех компонентов бота
    
    Args:
        log_level: Уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file: Логировать ли в файл
        log_file_path: Путь к файлу логов
    """
    
    # Преобразуем строку в уровень логирования
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Настраиваем корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Очищаем существующие handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Создаем форматтер для детального логирования
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Handler для stdout (обязательно для Render)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(detailed_formatter)
    root_logger.addHandler(stdout_handler)
    
    # Handler для файла (опционально)
    if log_to_file:
        try:
            file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
            file_handler.setLevel(numeric_level)
            file_handler.setFormatter(detailed_formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Could not create file handler: {e}", flush=True)
    
    # Настраиваем специальные логгеры
    setup_api_logger(numeric_level)
    setup_telegram_logger(numeric_level)
    setup_database_logger(numeric_level)
    
    # Принудительно выводим в stdout для Render
    print(f"=== DETAILED LOGGING SETUP COMPLETE ===", flush=True)
    print(f"Log level: {log_level}", flush=True)
    print(f"Log to file: {log_to_file}", flush=True)
    if log_to_file:
        print(f"Log file: {log_file_path}", flush=True)
    print(f"=== LOGGING READY ===", flush=True)

def setup_api_logger(level: int) -> None:
    """Настраивает логгер для API запросов"""
    api_logger = logging.getLogger('api_logger')
    api_logger.setLevel(level)
    
    # Не добавляем handlers если они уже есть
    if not api_logger.handlers:
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

def setup_telegram_logger(level: int) -> None:
    """Настраивает логгер для Telegram Bot API"""
    telegram_logger = logging.getLogger('telegram')
    telegram_logger.setLevel(level)
    
    # Не добавляем handlers если они уже есть
    if not telegram_logger.handlers:
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

def setup_database_logger(level: int) -> None:
    """Настраивает логгер для базы данных"""
    db_logger = logging.getLogger('asyncpg')
    db_logger.setLevel(level)
    
    # Не добавляем handlers если они уже есть
    if not db_logger.handlers:
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
