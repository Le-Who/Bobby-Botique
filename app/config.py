import os
import logging
import pytz
from typing import List, Dict, Any
from pydantic import BaseModel, ValidationError

def _load_and_clean_keys(env_var_name: str) -> List[str]:
    """
    Manually loads a comma-separated string from env, cleans it, and returns a list.
    This is the most robust way to handle env vars from hosting providers.
    """
    value = os.getenv(env_var_name)
    if not value:
        raise ValueError(f"Required environment variable '{env_var_name}' is not set.")

    # Clean the string from quotes and whitespace, then split.
    cleaned_v = value.strip().strip('"').strip("'")
    keys = [key.strip() for key in cleaned_v.split(',') if key.strip()]
    if not keys:
        raise ValueError(f"Environment variable '{env_var_name}' is set but contains no valid keys.")
    return keys

# We use BaseModel, NOT BaseSettings. We are not auto-loading from the environment.
class Settings(BaseModel):
    """
    Defines the shape and types of our settings for validation.
    Data is loaded manually and then passed here to be validated.
    """
    # --- CORE ---
    TELEGRAM_BOT_TOKEN: str
    GEMINI_API_KEYS: List[str]
    TAVILY_API_KEYS: List[str]
    DATABASE_URL: str
    ADMIN_ID: int
    PORT: int

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096

    # --- MODELS ---
    AVAILABLE_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"]
    DEFAULT_MODEL: str = "gemini-2.5-flash"
    QNA_MODEL: str = "gemini-2.5-flash-lite"
    RESEARCH_MODEL: str = "gemini-2.5-pro"
    URL_SELECTION_MODEL: str = "gemini-2.5-flash"

    # --- LIMITS ---
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97
    TAVILY_QNA_SEARCH_COST: int = 2
    TAVILY_ADVANCED_SEARCH_COST: int = 2
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    DAILY_LIMITS: Dict[str, int] = {
        "gemini-2.5-flash": 250,
        "gemini-2.5-pro": 100,
        "gemini-2.5-flash-lite": 1000,
    }

    # --- SAFETY ---
    # Режим безопасности (управляется через SAFETY_MODE env var)
    SAFETY_MODE: str = "auto"  # auto, standard, relaxed, disabled, aggressive

    # Основные настройки безопасности
    SAFETY_SETTINGS: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    ]

    # Альтернативные настройки для тестирования (менее строгие)
    SAFETY_SETTINGS_RELAXED: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
    ]

    # Полностью отключенные настройки безопасности (только для тестирования)
    SAFETY_SETTINGS_DISABLED: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # Агрессивные настройки безопасности (блокируют больше контента)
    SAFETY_SETTINGS_AGGRESSIVE: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_LOW_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_LOW_AND_ABOVE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_LOW_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_LOW_AND_ABOVE"},
    ]

    # --- DEBUG & LOGGING ---
    DEBUG_MODE: bool = False
    LOG_LEVEL: str = "INFO"  # DEBUG, INFO, WARNING, ERROR
    LOG_JSON: bool = False  # структурированные JSON-логи
    LOG_SAFETY_DECISIONS: bool = True  # Логировать решения по безопасности

    # --- PERFORMANCE ---
    ENABLE_CACHE: bool = True
    CACHE_TTL_HOURS: int = 72  # 3 дня
    MAX_RETRIES: int = 3
    REQUEST_TIMEOUT_SECONDS: int = 60
    ENABLE_PERSISTENT_QUEUE: bool = False  # Персистентная очередь

    # --- FEATURES ---
    ENABLE_SAFETY_FALLBACK: bool = True  # Автоматическое переключение настроек
    ENABLE_PROMPT_SIMPLIFICATION: bool = True  # Упрощение проблемных промптов
    ENABLE_SYSTEM_INSTRUCTION_FALLBACK: bool = True  # Отключение system_instruction при проблемах

    # --- RATE LIMITING ---
    USER_RATE_LIMIT_PER_MINUTE: int = 20

def load_settings() -> Settings:
    """
    Manually loads all settings from the environment and validates them
    using the Pydantic model. This is the most robust method.
    """
    try:
        # Manually load all values from the environment.
        raw_settings = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "DATABASE_URL": os.getenv("DATABASE_URL"),
            "ADMIN_ID": os.getenv("ADMIN_ID"),
            "PORT": os.getenv("PORT", 10000), # Provide a default for PORT
            "GEMINI_API_KEYS": _load_and_clean_keys("GEMINI_API_KEYS"),
            "TAVILY_API_KEYS": _load_and_clean_keys("TAVILY_API_KEYS"),

            # Новые настройки безопасности
            "SAFETY_MODE": os.getenv("SAFETY_MODE", "auto"),
            "DEBUG_MODE": os.getenv("DEBUG_MODE", "false").lower() == "true",
            "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
            "LOG_JSON": os.getenv("LOG_JSON", "false").lower() == "true",
            "LOG_SAFETY_DECISIONS": os.getenv("LOG_SAFETY_DECISIONS", "true").lower() == "true",

            # Настройки производительности
            "ENABLE_CACHE": os.getenv("ENABLE_CACHE", "true").lower() == "true",
            "CACHE_TTL_HOURS": int(os.getenv("CACHE_TTL_HOURS", "72")),
            "MAX_RETRIES": int(os.getenv("MAX_RETRIES", "3")),
            "REQUEST_TIMEOUT_SECONDS": int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60")),
            "ENABLE_PERSISTENT_QUEUE": os.getenv("ENABLE_PERSISTENT_QUEUE", "false").lower() == "true",

            # Настройки функций
            "ENABLE_SAFETY_FALLBACK": os.getenv("ENABLE_SAFETY_FALLBACK", "true").lower() == "true",
            "ENABLE_PROMPT_SIMPLIFICATION": os.getenv("ENABLE_PROMPT_SIMPLIFICATION", "true").lower() == "true",
            "ENABLE_SYSTEM_INSTRUCTION_FALLBACK": os.getenv("ENABLE_SYSTEM_INSTRUCTION_FALLBACK", "true").lower() == "true",
            "USER_RATE_LIMIT_PER_MINUTE": int(os.getenv("USER_RATE_LIMIT_PER_MINUTE", "20")),
        }

        # Use the Pydantic model ONLY for validation of the manually loaded data.
        settings = Settings(**raw_settings)

        # Настраиваем логирование на основе конфигурации
        _setup_logging(settings)

        return settings
    except (ValidationError, ValueError) as e:
        # Catch errors from both Pydantic and our manual functions.
        print(f"FATAL: Could not load settings. Please check your environment variables. Error: {e}")
        exit(1)

def _setup_logging(settings: Settings):
    """Настраивает логирование на основе конфигурации"""
    import logging
    import sys

    # Устанавливаем уровень логирования
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    if settings.LOG_JSON:
        try:
            from .logging_utils import JsonFormatter, RequestContextFilter
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(JsonFormatter())
            root = logging.getLogger()
            root.setLevel(log_level)
            # очищаем дефолтные хендлеры basicConfig, если есть
            for h in list(root.handlers):
                root.removeHandler(h)
            root.addHandler(handler)
            root.addFilter(RequestContextFilter())
        except Exception:
            logging.basicConfig(
                level=log_level,
                format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
    else:
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

    # Логируем загруженную конфигурацию
    logging.info(f"=== CONFIGURATION LOADED ===")
    logging.info(f"Safety Mode: {settings.SAFETY_MODE}")
    logging.info(f"Debug Mode: {settings.DEBUG_MODE}")
    logging.info(f"Log Level: {settings.LOG_LEVEL}")
    logging.info(f"Log JSON: {settings.LOG_JSON}")
    logging.info(f"Cache Enabled: {settings.ENABLE_CACHE}")
    logging.info(f"Cache TTL: {settings.CACHE_TTL_HOURS} hours")
    logging.info(f"Max Retries: {settings.MAX_RETRIES}")
    logging.info(f"Request Timeout: {settings.REQUEST_TIMEOUT_SECONDS} seconds")
    logging.info(f"Persistent Queue: {settings.ENABLE_PERSISTENT_QUEUE}")
    logging.info(f"Safety Fallback: {settings.ENABLE_SAFETY_FALLBACK}")
    logging.info(f"Prompt Simplification: {settings.ENABLE_PROMPT_SIMPLIFICATION}")
    logging.info(f"System Instruction Fallback: {settings.ENABLE_SYSTEM_INSTRUCTION_FALLBACK}")

def get_safety_settings(mode: str = None) -> List[Dict[str, str]]:
    """
    Возвращает настройки безопасности в зависимости от режима

    Args:
        mode: Режим безопасности (auto, standard, relaxed, disabled, aggressive)
              Если None, используется SAFETY_MODE из настроек

    Returns:
        List[Dict[str, str]]: Настройки безопасности
    """
    if mode is None:
        mode = get_current_safety_mode()

    safety_mapping = {
        "standard": settings.SAFETY_SETTINGS,
        "relaxed": settings.SAFETY_SETTINGS_RELAXED,
        "disabled": settings.SAFETY_SETTINGS_DISABLED,
        "aggressive": settings.SAFETY_SETTINGS_AGGRESSIVE,
        "auto": settings.SAFETY_SETTINGS,  # По умолчанию стандартные
    }

    return safety_mapping.get(mode, settings.SAFETY_SETTINGS)

def get_current_safety_mode() -> str:
    """Получает текущий режим безопасности из базы данных или конфигурации"""
    try:
        # Пытаемся получить из базы данных синхронно
        return get_setting_from_db("SAFETY_MODE", settings.SAFETY_MODE)
    except Exception:
        # Если не удалось, возвращаем значение по умолчанию
        return settings.SAFETY_MODE

async def get_setting_from_db_async(setting_name: str, default_value: Any = None) -> Any:
    """
    Асинхронно получает значение настройки из базы данных

    Args:
        setting_name: Название настройки
        default_value: Значение по умолчанию, если настройка не найдена

    Returns:
        Any: Значение настройки или default_value
    """
    try:
        from .database import db_query

        result = await db_query("SELECT value FROM bot_settings WHERE setting_name = $1", (setting_name,))

        if result and result[0]:
            value = result[0]['value']
            # Преобразуем строку в соответствующий тип
            if isinstance(default_value, bool):
                return value.lower() == 'true'
            elif isinstance(default_value, int):
                return int(value)
            elif isinstance(default_value, float):
                return float(value)
            else:
                return value

    except Exception as e:
        logging.warning(f"Could not get setting {setting_name} from database: {e}")

    # Возвращаем значение по умолчанию
    return default_value

def get_setting_from_db(setting_name: str, default_value: Any = None) -> Any:
    """
    Синхронная обертка для получения настройки из базы данных
    ВНИМАНИЕ: Используйте только в синхронном контексте!

    Args:
        setting_name: Название настройки
        default_value: Значение по умолчанию, если настройка не найдена

    Returns:
        Any: Значение настройки или default_value
    """
    try:
        import asyncio

        # Пытаемся получить текущий event loop
        try:
            loop = asyncio.get_running_loop()
            # Если мы в асинхронном контексте, логируем предупреждение
            logging.warning(f"get_setting_from_db called in async context. Use get_setting_from_db_async instead.")
            return default_value
        except RuntimeError:
            # Мы не в асинхронном контексте, можно создать новый loop
            pass

        # Создаем новый event loop для синхронного вызова
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(
                get_setting_from_db_async(setting_name, default_value)
            )
            return result
        finally:
            loop.close()

    except Exception as e:
        logging.warning(f"Could not get setting {setting_name} from database: {e}")
        return default_value

def get_safety_mode_description() -> str:
    """Возвращает описание доступных режимов безопасности"""
    return """
Доступные режимы безопасности:

🔒 standard - Стандартные настройки (BLOCK_MEDIUM_AND_ABOVE)
   Блокирует контент среднего и высокого уровня вреда

🟡 relaxed - Расслабленные настройки (BLOCK_ONLY_HIGH)
   Блокирует только контент высокого уровня вреда

🟢 disabled - Отключенные настройки (BLOCK_NONE)
   Не блокирует контент (только для тестирования)

🔴 aggressive - Агрессивные настройки (BLOCK_LOW_AND_ABOVE)
   Блокирует контент низкого, среднего и высокого уровня

🔄 auto - Автоматический режим
   Автоматически переключается между режимами при проблемах

Установка: SAFETY_MODE=relaxed в переменных окружения
"""

# --- TIMEZONES ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
# Create the one and only settings object for the app.
settings = load_settings()
