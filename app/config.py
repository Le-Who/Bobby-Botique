import os
import pytz
import time
import asyncio
import logging
import json
import hashlib
from typing import List, Dict, Callable, Any, Optional
from pydantic import BaseModel, ValidationError


def _load_int_env(env_var_name: str, required: bool = True):
    raw = os.getenv(env_var_name)
    if raw is None or raw == "":
        if required:
            raise ValueError(
                f"Required environment variable '{env_var_name}' is not set."
            )
        return None
    cleaned = raw.strip().strip('"').strip("'").strip()
    return int(cleaned)


def _load_and_clean_keys(env_var_name: str, required: bool = True) -> List[str]:
    """
    Manually loads a comma-separated string from env, cleans it, and returns a list.
    This is the most robust way to handle env vars from hosting providers.

    Args:
        env_var_name: Name of environment variable
        required: If True, raises error if not set. If False, returns empty list.
    """
    value = os.getenv(env_var_name)
    if not value:
        if required:
            raise ValueError(
                f"Required environment variable '{env_var_name}' is not set."
            )
        return []

    # Clean the string from quotes and whitespace, then split.
    cleaned_v = value.strip().strip('"').strip("'")
    keys = [key.strip() for key in cleaned_v.split(",") if key.strip()]
    if required and not keys:
        raise ValueError(
            f"Environment variable '{env_var_name}' is set but contains no valid keys."
        )
    return keys


def _load_daily_limits() -> Dict[str, int]:
    """
    Загружает DAILY_LIMITS from env переменной to formatе JSON or компактном формате.

    Формат в env (JSON, рекомендуется):
    DAILY_LIMITS='{"gemini-exp-1206": 250, "gemini-flash-latest": 15}'

    Или компактный формат:
    DAILY_LIMITS='gemini-exp-1206:250,gemini-flash-latest:15'

    Returns:
        Dict[str, int]: Словарь с limitами for моделей
    """
    value = os.getenv("DAILY_LIMITS")

    # Значения by default
    default_limits = {
        "gemini-2.5-flash": 15,
        "gemini-2.5-flash-latest": 15,
        "gemini-2.5-flash-lite": 15,
        "gemini-flash-lite-latest": 15,
    }

    if not value:
        return default_limits

    try:
        cleaned = value.strip().strip('"').strip("'")

        # Пробуем JSON формат
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # If не JSON, пробуем компактный формат: "model1:limit1,model2:limit2"
            result = {}
            for item in cleaned.split(","):
                if ":" in item:
                    model, limit = item.split(":", 1)
                    result[model.strip()] = int(limit.strip())
            if result:
                return result
            else:
                raise ValueError("No valid limits found")
    except (ValueError, AttributeError, json.JSONDecodeError) as e:
        logging.warning("Failed to parse DAILY_LIMITS from env: %s. Using defaults.", e)
        return default_limits


def get_model_hash(model_name: str) -> str:
    """
    Генерирует короткий хэш models (8 символов) for использования в callback_data.

    Args:
        model_name: Полное имя models

    Returns:
        str: 8-символьный хэш models
    """
    return hashlib.md5(model_name.encode()).hexdigest()[:8]


# We use BaseModel, NOT BaseSettings. We are not auto-loading from the environment.
class Settings(BaseModel):
    """
    Defines the shape and types of our settings for validation.
    Data is loaded manually and then passed here to be validated.
    """

    # --- CORE ---
    TELEGRAM_BOT_TOKEN: str
    ADMIN_SECRET: Optional[str] = None
    GEMINI_API_KEYS: List[str]
    TAVILY_API_KEYS: List[str]
    OPENROUTER_API_KEYS: List[str] = []  # Optional, by default empty list
    DATABASE_URL: str
    ADMIN_ID: int
    PORT: int
    ENABLE_WEB_SERVER: bool = True  # Default to True for cloud deployments

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096

    # --- MODELS ---
    # Модели загружаются from env переменных, значения by default используются if не указаны
    AVAILABLE_MODELS: List[str] = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-3-flash-preview",
    ]
    DEFAULT_MODEL: str = "gemini-flash-latest"
    QNA_MODEL: str = "gemini-2.5-flash-lite"
    RESEARCH_MODEL: str = "gemini-2.5-flash"
    URL_SELECTION_MODEL: str = "gemini-flash-latest"

    # --- OPENROUTER MODELS ---
    # Модели загружаются from env переменных, значения by default используются if не указаны
    OPENROUTER_AVAILABLE_MODELS: List[str] = []
    OPENROUTER_DEFAULT_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_QNA_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_RESEARCH_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_URL_SELECTION_MODEL: str = "stepfun/step-3.5-flash:free"

    # --- API PROVIDER SELECTION ---
    USE_OPENROUTER: bool = (
        False  # По умолчанию use Gemini, можно переkeysть на OpenRouter
    )

    # --- LIMITS ---
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97
    TAVILY_QNA_SEARCH_COST: int = 2
    TAVILY_ADVANCED_SEARCH_COST: int = 2
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    # DAILY_LIMITS загружается from env переменной DAILY_LIMITS to formatе JSON
    DAILY_LIMITS: Dict[str, int] = {
        "gemini-2.5-flash": 15,
        "gemini-flash-latest": 15,
        "gemini-2.5-flash-lite": 15,
        "gemini-flash-lite-latest": 15,
        "gemini-3-flash-preview": 15,
    }
    ALERT_COOLDOWN_SECONDS: int = 3600
    MAX_DOCUMENTS_PER_USER: int = 5

    # --- SAFETY ---
    SAFETY_SETTINGS: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # --- DEFAULT SYSTEM PROMPT ---
    DEFAULT_SYSTEM_PROMPT: str = r"""# РОЛЬ И ЗАДАЧА
Ты — полезный ИИ-ассистент for Telegram. Твоя задача — отвеchatь на вопросы user, используя правильное форматирование и предоставляя точную, полезную информацию.

# КОНТЕКСТ
Ты работаешь в Telegram-боте. Твои responseы должны быть отформатированы в **стандартном Markdown** (не MarkdownV2!).

# ПОШАГОВЫЕ ИНСТРУКЦИИ
1. **Проаналfromируй вопрос user**
2. **Сформулируй четкий, структурированный response**
3. **Примени стандартное Markdown форматирование**
4. **Проверь корректность математических выражений**
5. **Убедись, что НЕТ лишнего экранирования**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Технический вопрос
**Вопрос:** "Что такое Python?"
**Правильный response:**
**Python** — это высокоуровневый язык программирования.

_Основные особенности:_
- Простой синтаксис
- Большая библиотека

[Подробнее](https://python.org)

## Пример 2: Математический вопрос
**Вопрос:** "Как решить x² + 2x + 1 = 0?"
**Правильный response:**
Решение уравнения `x² + 2x + 1 = 0`:
1. Дискриминант: `D = 0`
2. Корень: `x = -1`

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО (Стандартный Markdown)
- `**жирный text**` or `__жирный text__`
- `*курсив*` or `_курсив_`
- `` `код` `` for технических терминов
- `[text ссылки](URL)` for ссылок
- `- ` for списков
- `> ` for цитат

## ❌ ЗАПРЕЩЕНО
- **MarkdownV2 экранирование**: НЕ пиши `\.`, `\-`, `\!`, `\(`, `\)`. Пиши просто `.`, `-`, `!`, `(`, `)`.
- **HTML теги**: НЕ используй `<b>`, `<i>`, `<br>`.
- **LaTeX**: НЕ используй `$...$`.

# ФОРМАТИРОВАНИЕ МАТЕМАТИКИ
Пиши формулы как обычный text or код:
- `2 * 2 = 4`
- `x^2`
- `sqrt(4) = 2`

# СТИЛЬ ОБЩЕНИЯ
- Будь полезным и точным
- Структурируй информацию логично
- Используй onмеры
- Будь дружелюбным

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой убедись, что:
- [ ] Использован стандартный Markdown
- [ ] НЕТ экранирования спецсимволов обратным слешем
- [ ] Нет HTML тегов
- [ ] Ответ полезен и структурирован"""

    # COMPACT_SYSTEM_PROMPT (оптимfromированная версия) ---
    # Компактная версия базового промпта for экономии tokenов
    # Используется on наличии roles or for простых requestов
    COMPACT_SYSTEM_PROMPT: str = r"""# РОЛЬ
ИИ-ассистент for Telegram. Отвечай точно, используя **Standard Markdown**.

# ФОРМАТИРОВАНИЕ
✅ `**жирный**`, `_курсив_`, `` `код` ``, `[ссылка](URL)`, `- списки`
❌ HTML теги, MarkdownV2 (`\.`, `\-`), LaTeX

# МАТЕМАТИКА
Обычный text: `2 * 3 = 6`, `x^2`, `sqrt(2)`

# ЭКРАНИРОВАНИЕ
⛔️ **НЕ ЭКРАНИРУЙ** знаки препинания! Пиши `.` `!` `(` `)` как есть.

# СТИЛЬ
Полезный, структурированный, дружелюбный."""


def load_settings() -> Settings:
    """
    Manually loads all settings from the environment and validates them
    using the Pydantic model. This is the most robust method.
    """
    try:
        # Значения by default for моделей
        default_gemini_models = [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-3-flash-preview",
        ]
        default_openrouter_models = []

        # Manually load all values from the environment.
        raw_settings = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "ADMIN_SECRET": os.getenv("ADMIN_SECRET"),
            "DATABASE_URL": os.getenv("DATABASE_URL"),
            "ADMIN_ID": _load_int_env("ADMIN_ID"),
            "PORT": os.getenv("PORT", "10000"),  # Provide a default for PORT
            "ENABLE_WEB_SERVER": os.getenv("ENABLE_WEB_SERVER", "true").lower()
            == "true",
            "GEMINI_API_KEYS": _load_and_clean_keys("GEMINI_API_KEYS"),
            "TAVILY_API_KEYS": _load_and_clean_keys("TAVILY_API_KEYS"),
            "OPENROUTER_API_KEYS": _load_and_clean_keys(
                "OPENROUTER_API_KEYS", required=False
            ),
            # Load models from env or use значения by default
            "AVAILABLE_MODELS": _load_and_clean_keys(
                "GEMINI_AVAILABLE_MODELS", required=False
            )
            or default_gemini_models,
            "OPENROUTER_AVAILABLE_MODELS": _load_and_clean_keys(
                "OPENROUTER_AVAILABLE_MODELS", required=False
            )
            or default_openrouter_models,
            "DEFAULT_MODEL": os.getenv("DEFAULT_MODEL", "gemini-flash-latest"),
            "QNA_MODEL": os.getenv("QNA_MODEL", "gemini-2.5-flash-lite"),
            "RESEARCH_MODEL": os.getenv("RESEARCH_MODEL", "gemini-2.5-pro"),
            "URL_SELECTION_MODEL": os.getenv(
                "URL_SELECTION_MODEL", "gemini-flash-latest"
            ),
            "OPENROUTER_DEFAULT_MODEL": os.getenv(
                "OPENROUTER_DEFAULT_MODEL", "stepfun/step-3.5-flash:free"
            ),
            "OPENROUTER_QNA_MODEL": os.getenv(
                "OPENROUTER_QNA_MODEL", "stepfun/step-3.5-flash:free"
            ),
            "OPENROUTER_RESEARCH_MODEL": os.getenv(
                "OPENROUTER_RESEARCH_MODEL", "stepfun/step-3.5-flash:free"
            ),
            "OPENROUTER_URL_SELECTION_MODEL": os.getenv(
                "OPENROUTER_URL_SELECTION_MODEL", "stepfun/step-3.5-flash:free"
            ),
            "DAILY_LIMITS": _load_daily_limits(),
        }

        # Validation: проверяем, что DEFAULT_MODEL и другие константы есть в списках моделей
        settings_obj = Settings(**raw_settings)

        # Check Gemini models
        if settings_obj.DEFAULT_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                f"DEFAULT_MODEL '{settings_obj.DEFAULT_MODEL}' not in AVAILABLE_MODELS. Adding it."
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.DEFAULT_MODEL)

        if settings_obj.QNA_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                f"QNA_MODEL '{settings_obj.QNA_MODEL}' not in AVAILABLE_MODELS. Adding it."
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.QNA_MODEL)

        if settings_obj.RESEARCH_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                f"RESEARCH_MODEL '{settings_obj.RESEARCH_MODEL}' not in AVAILABLE_MODELS. Adding it."
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.RESEARCH_MODEL)

        # Check OpenRouter models
        if (
            settings_obj.OPENROUTER_DEFAULT_MODEL
            not in settings_obj.OPENROUTER_AVAILABLE_MODELS
        ):
            logging.warning(
                f"OPENROUTER_DEFAULT_MODEL '{settings_obj.OPENROUTER_DEFAULT_MODEL}' not in OPENROUTER_AVAILABLE_MODELS. Adding it."
            )
            settings_obj.OPENROUTER_AVAILABLE_MODELS.append(
                settings_obj.OPENROUTER_DEFAULT_MODEL
            )

        return settings_obj
    except (ValidationError, ValueError) as e:
        # Catch errors from both Pydantic and our manual functions.
        # Catch errors from both Pydantic and our manual functions.
        error_msg = f"FATAL: Could not load settings. Please check your environment variables. Error: {e}"
        print(error_msg)
        raise ValueError(error_msg)


# --- TIMEZONES ---
# Кэшируем временные зоны for предотвращения requestов к pg_timezone_names
PACIFIC_TZ = pytz.timezone("US/Pacific")
KYIV_TZ = pytz.timezone("Europe/Kyiv")
UTC_TZ = pytz.UTC  # Используем константу instead of pytz.utc

# --- LAZY LOADING SETTINGS ---
_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Returns settings instance with lazy loading.
    This prevents initialization errors during import.
    """
    global _settings_instance
    if _settings_instance is None:
        try:
            _settings_instance = load_settings()
        except Exception as e:
            logging.error("Failed to load settings: %s", e, exc_info=True)
            raise
    return _settings_instance


# Backward compatibility - use lazy loading
def get_settings_safe() -> Optional[Settings]:
    """
    Safe version that returns None if settings cannot be loaded.
    Useful for testing and development.
    """
    try:
        return get_settings()
    except Exception:
        return None


# --- SINGLETON INSTANCE ---
# Create the one and only settings object for the app.
# Use lazy loading to prevent import errors
try:
    settings = get_settings()
except Exception:
    # During development/testing, allow None settings
    settings = None


class ConfigManager:
    """Manages configuration with hot reloading capability."""

    def __init__(self):
        self._settings = get_settings_safe()
        self._last_reload = time.time()
        self._reload_interval = 300  # 5 minutes
        self._watchers: List[Callable] = []
        self._lock = asyncio.Lock()
        self._reload_task: Optional[asyncio.Task] = None

    @property
    def settings(self) -> Settings:
        """Returns current settings, reloading if necessary."""
        if self._settings is None:
            self._settings = get_settings()
        current_time = time.time()
        if current_time - self._last_reload > self._reload_interval:
            # Debounce: update timestamp eagerly to prevent overlapping reloads
            self._last_reload = current_time
            # Only create a new task if the previous one is done
            if self._reload_task is None or self._reload_task.done():
                try:
                    self._reload_task = asyncio.create_task(self._reload_config())
                except RuntimeError:
                    pass  # No running event loop (e.g. during tests)
        return self._settings

    async def _reload_config(self) -> None:
        """Reloads configuration from environment and migrates users with invalid models."""
        async with self._lock:
            try:
                new_settings = get_settings()

                # Check if any critical settings changed
                critical_changed = (
                    new_settings.TELEGRAM_BOT_TOKEN != self._settings.TELEGRAM_BOT_TOKEN
                    or new_settings.DATABASE_URL != self._settings.DATABASE_URL
                    or new_settings.ADMIN_ID != self._settings.ADMIN_ID
                )

                if critical_changed:
                    logging.warning(
                        "Critical configuration changed, restart may be required"
                    )

                # === ВАЛИДАЦИЯ И МИГРАЦИЯ АКТИВНЫХ ПОЛЬЗОВАТЕЛЕЙ ===
                try:
                    from app.repos.chats import migrate_invalid_models

                    all_available_models = set()
                    if new_settings.AVAILABLE_MODELS:
                        all_available_models.update(new_settings.AVAILABLE_MODELS)
                    if new_settings.OPENROUTER_AVAILABLE_MODELS:
                        all_available_models.update(
                            new_settings.OPENROUTER_AVAILABLE_MODELS
                        )

                    migrated_count = await migrate_invalid_models(
                        available_models=all_available_models,
                        default_gemini_model=new_settings.DEFAULT_MODEL,
                        default_openrouter_model=new_settings.OPENROUTER_DEFAULT_MODEL,
                    )
                except Exception as migration_error:
                    migrated_count = 0
                    # Не прерываем перезагрузку on ошибке миграции
                    logging.error("Error during user migration: %s", migration_error)

                # Check, что DEFAULT_MODEL существует
                all_available_models_check = set()
                if new_settings.AVAILABLE_MODELS:
                    all_available_models_check.update(new_settings.AVAILABLE_MODELS)
                if new_settings.OPENROUTER_AVAILABLE_MODELS:
                    all_available_models_check.update(
                        new_settings.OPENROUTER_AVAILABLE_MODELS
                    )

                if new_settings.DEFAULT_MODEL not in all_available_models_check:
                    logging.error(
                        f"DEFAULT_MODEL '{new_settings.DEFAULT_MODEL}' not in AVAILABLE_MODELS!"
                    )
                    raise ValueError("DEFAULT_MODEL must be in AVAILABLE_MODELS")

                # Update settings
                old_settings = self._settings
                self._settings = new_settings
                self._last_reload = time.time()

                # Notify watchers
                await self._notify_watchers(old_settings, new_settings)

                logging.info(
                    f"Configuration reloaded successfully. Migrated {migrated_count} users."
                )

            except Exception as e:
                logging.error("Failed to reload configuration: %s", e, exc_info=True)
                # Не прерываем работу on ошибке перезагрузки конфигурации
                # Система продолжит работать со старыми настройками

    def add_watcher(self, callback: Callable[[Settings, Settings], None]) -> None:
        """Adds a configuration change watcher."""
        self._watchers.append(callback)

    async def _notify_watchers(
        self, old_settings: Settings, new_settings: Settings
    ) -> None:
        """Notifies all watchers of configuration changes."""
        for watcher in self._watchers:
            try:
                if asyncio.iscoroutinefunction(watcher):
                    await watcher(old_settings, new_settings)
                else:
                    watcher(old_settings, new_settings)
            except Exception as e:
                logging.error("Configuration watcher error: %s", e, exc_info=True)

    async def force_reload(self) -> None:
        """Forces immediate configuration reload."""
        await self._reload_config()

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Gets a specific setting value."""
        return getattr(self.settings, key, default)

    def update_setting(self, key: str, value: Any) -> None:
        """Updates a setting value (for testing/debugging)."""
        if hasattr(self.settings, key):
            setattr(self.settings, key, value)
            logging.info("Setting updated: %s = %s", key, value)
        else:
            logging.warning("Unknown setting: %s", key)


# Global config manager instance
config_manager = ConfigManager()


# Convenience functions for common settings
def get_bot_token() -> str:
    """Returns bot token."""
    return config_manager.get_setting("TELEGRAM_BOT_TOKEN")


def get_database_url() -> str:
    """Returns database URL."""
    return config_manager.get_setting("DATABASE_URL")


def get_admin_id() -> int:
    return int(config_manager.get_setting("ADMIN_ID"))


def get_gemini_keys() -> List[str]:
    """Returns Gemini API keys."""
    return config_manager.get_setting("GEMINI_API_KEYS", [])


def get_tavily_keys() -> List[str]:
    """Returns Tavily API keys."""
    return config_manager.get_setting("TAVILY_API_KEYS", [])


def get_openrouter_keys() -> List[str]:
    """Returns OpenRouter API keys."""
    return config_manager.get_setting("OPENROUTER_API_KEYS", [])


def get_use_openrouter() -> bool:
    """Returns whether to use OpenRouter instead of Gemini."""
    return config_manager.get_setting("USE_OPENROUTER", False)
