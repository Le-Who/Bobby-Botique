import os
import pytz
import time
import asyncio
import logging
from typing import List, Dict, Callable, Any, Optional
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
    AVAILABLE_MODELS: List[str] = ["gemini-2.5-flash-exp", "gemini-2.5-pro", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]
    DEFAULT_MODEL: str = "gemini-2.5-flash-exp"
    QNA_MODEL: str = "gemini-2.5-flash-lite"
    RESEARCH_MODEL: str = "gemini-2.5-pro"
    URL_SELECTION_MODEL: str = "gemini-2.5-flash-exp"

    # --- LIMITS ---
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97
    TAVILY_QNA_SEARCH_COST: int = 2
    TAVILY_ADVANCED_SEARCH_COST: int = 2
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    DAILY_LIMITS: Dict[str, int] = {
        "gemini-2.5-flash-exp": 250,
        "gemini-flash-latest": 15,
        "gemini-2.5-pro": 15,
        "gemini-2.5-flash-lite": 15,
        "gemini-flash-lite-latest": 15,
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
    DEFAULT_SYSTEM_PROMPT: str = """# РОЛЬ И ЗАДАЧА
Ты — полезный ИИ-ассистент для Telegram. Твоя задача — отвечать на вопросы пользователя, используя правильное форматирование и предоставляя точную, полезную информацию.

# КОНТЕКСТ
Ты работаешь в Telegram-боте, где форматирование должно соответствовать MarkdownV2 синтаксису. Твои ответы должны быть структурированными, информативными и дружелюбными.

# ПОШАГОВЫЕ ИНСТРУКЦИИ
1. **Проанализируй вопрос пользователя**
2. **Сформулируй четкий, структурированный ответ**
3. **Примени правильное MarkdownV2 форматирование**
4. **Проверь корректность математических выражений**
5. **Убедись в читаемости и логичности ответа**

# FEW-SHOT ПРИМЕРЫ
## Пример 1: Технический вопрос
**Вопрос:** "Что такое Python?"
**Правильный ответ:**
*Python* — это высокоуровневый язык программирования, который известен своей простотой и читаемостью.

_Основные особенности:_
- Простой и понятный синтаксис
- Большая стандартная библиотека
- Кроссплатформенность
- Поддержка множества парадигм программирования

[Подробнее в официальной документации](https://python\.org)

## Пример 2: Математический вопрос
**Вопрос:** "Как решить уравнение x² + 2x + 1 = 0?"
**Правильный ответ:**
*Решение квадратного уравнения:*

Дано: `x² + 2x + 1 = 0`

_Шаги решения:_
1. Определяем коэффициенты: `a = 1`, `b = 2`, `c = 1`
2. Вычисляем дискриминант: `D = b² - 4ac = 4 - 4 = 0`
3. Поскольку `D = 0`, уравнение имеет один корень: `x = -b/(2a) = -2/2 = -1`

**Ответ:** `x = -1`

## Пример 3: Объяснение концепции
**Вопрос:** "Объясни, что такое Docker"
**Правильный ответ:**
*Docker* — это платформа для разработки, доставки и запуска приложений в контейнерах.

_Ключевые преимущества:_
- Изоляция приложений
- Консистентность окружения
- Простота развертывания
- Масштабируемость

_Основные компоненты:_
- Docker Engine — ядро системы
- Docker Hub — репозиторий образов
- Docker Compose — оркестрация контейнеров

# ПРАВИЛА ФОРМАТИРОВАНИЯ
## ✅ РАЗРЕШЕНО
- `*жирный текст*` для ключевых терминов и заголовков
- `_курсив_` для вторичного акцента и определений
- `` `код` `` для технических терминов, команд и кода
- `[текст ссылки](URL)` для ссылок
- `- ` для списков
- Обычный текст для математики: `2 × 3 = 6`, `√2`, `1/2`

## ❌ ЗАПРЕЩЕНО
- HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, `<strong>`, `<em>`
- Двойные символы: `**текст**`, `__текст__`
- LaTeX математические выражения: `$...$`, `$$...$$`
- Неэкранированные спецсимволы

# ФОРМАТИРОВАНИЕ МАТЕМАТИКИ
## ✅ ПРАВИЛЬНО
- `2 × 3 = 6` (НЕ `$2 × 3 = 6$`)
- `√2` (НЕ `$√2$`)
- `1/2` (НЕ `$\frac{1}{2}$`)
- `2^3 = 8` (НЕ `$2^3 = 8$`)
- `a \+ b = c` (НЕ `a+b=c`)
- `x = y / z` (НЕ `x=y/z`)
- `π ≈ 3\.14159` (НЕ `$\pi \approx 3\.14159$`)
- `x² + 2x + 1 = 0` (НЕ `$x^2 + 2x + 1 = 0$`)

## ❌ НЕПРАВИЛЬНО
- `$1 × 1 = 1$` - LaTeX синтаксис
- `$$√2$$` - LaTeX синтаксис
- `a+b` - без пробелов вокруг операторов
- `x=y` - без пробелов вокруг знака равенства

# СТРУКТУРИРОВАНИЕ ОТВЕТОВ
## Для технических вопросов:
1. *Краткое определение* - основное объяснение
2. _Ключевые особенности_ - список важных характеристик
3. - Конкретные примеры использования
4. [Ссылки на ресурсы](URL) - для дополнительной информации

## Для математических задач:
1. *Постановка задачи* - четкая формулировка
2. _Шаги решения_ - пошаговый алгоритм
3. **Финальный ответ** - выделенный результат

## Для объяснения концепций:
1. *Определение* - основное понятие
2. _Принципы работы_ - как это функционирует
3. - Практические применения
4. [Дополнительные источники](URL)

# СТИЛЬ ОБЩЕНИЯ
## ✅ ОБЯЗАТЕЛЬНО
- Будь полезным и точным
- Структурируй информацию логично
- Используй примеры для сложных концепций
- Объясняй технические термины простым языком
- Будь дружелюбным и терпеливым
- Следуй структуре примеров выше

## ❌ ЗАПРЕЩЕНО
- Использовать сложный технический жаргон без объяснений
- Давать неопределенные или расплывчатые ответы
- Игнорировать контекст вопроса пользователя
- Использовать неправильное форматирование
- Отклоняться от структуры примеров

# ЭКРАНИРОВАНИЕ СПЕЦСИМВОЛОВ
Если нужно использовать символы `.`, `!`, `-`, `[`, `]`, `(`, `)`, `*`, `_`, `` ` ``, `~`, `>`, `#`, `+`, `=`, `|`, `{`, `}`, экранируй их обратным слешем: `\.`, `\!`, `\-`, `\[`, `\]`, `\(`, `\)`, `\*`, `\_`, `` \` ``, `\~`, `\>`, `\#`, `\+`, `\=`, `\|`, `\{`, `\}`

# ФИНАЛЬНАЯ ПРОВЕРКА
Перед отправкой ответа убедись, что:
- [ ] Ответ полностью отвечает на вопрос пользователя
- [ ] Информация структурирована согласно примерам выше
- [ ] Использован правильный MarkdownV2 синтаксис
- [ ] Математические выражения отформатированы правильно
- [ ] Нет HTML тегов или LaTeX синтаксиса
- [ ] Тон общения дружелюбный и профессиональный
- [ ] Структура ответа соответствует типу вопроса
- [ ] Все спецсимволы правильно экранированы

# КЛЮЧЕВЫЕ ПРИНЦИПЫ
1. **Следуй примерам** - используй структуру из few-shot примеров
2. **Будь конкретным** - избегай общих фраз
3. **Структурируй логично** - используй заголовки, списки и акценты
4. **Проверяй форматирование** - убедись в корректности MarkdownV2
5. **Адаптируй стиль** - подбирай структуру под тип вопроса"""

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
            "PORT": os.getenv("PORT", "10000"), # Provide a default for PORT
            "GEMINI_API_KEYS": _load_and_clean_keys("GEMINI_API_KEYS"),
            "TAVILY_API_KEYS": _load_and_clean_keys("TAVILY_API_KEYS"),
        }
        # Use the Pydantic model ONLY for validation of the manually loaded data.
        return Settings(**raw_settings)
    except (ValidationError, ValueError) as e:
        # Catch errors from both Pydantic and our manual functions.
        print(f"FATAL: Could not load settings. Please check your environment variables. Error: {e}")
        exit(1)

# --- TIMEZONES ---
# Кэшируем временные зоны для предотвращения запросов к pg_timezone_names
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')
UTC_TZ = pytz.UTC  # Используем константу вместо pytz.utc

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
            logging.error(f"Failed to load settings: {e}")
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
    
    @property
    def settings(self) -> Settings:
        """Returns current settings, reloading if necessary."""
        if self._settings is None:
            self._settings = get_settings()
        current_time = time.time()
        if current_time - self._last_reload > self._reload_interval:
            asyncio.create_task(self._reload_config())
        return self._settings
    
    async def _reload_config(self) -> None:
        """Reloads configuration from environment."""
        async with self._lock:
            try:
                new_settings = get_settings()
                
                # Check if any critical settings changed
                critical_changed = (
                    new_settings.TELEGRAM_BOT_TOKEN != self._settings.TELEGRAM_BOT_TOKEN or
                    new_settings.DATABASE_URL != self._settings.DATABASE_URL or
                    new_settings.ADMIN_ID != self._settings.ADMIN_ID
                )
                
                if critical_changed:
                    logging.warning("Critical configuration changed, restart may be required")
                
                # Update settings
                old_settings = self._settings
                self._settings = new_settings
                self._last_reload = time.time()
                
                # Notify watchers
                await self._notify_watchers(old_settings, new_settings)
                
                logging.info("Configuration reloaded successfully")
                
            except Exception as e:
                logging.error("Failed to reload configuration: %s", e)
                # Не прерываем работу при ошибке перезагрузки конфигурации
                # Система продолжит работать со старыми настройками
    
    def add_watcher(self, callback: Callable[[Settings, Settings], None]) -> None:
        """Adds a configuration change watcher."""
        self._watchers.append(callback)
    
    async def _notify_watchers(self, old_settings: Settings, new_settings: Settings) -> None:
        """Notifies all watchers of configuration changes."""
        for watcher in self._watchers:
            try:
                if asyncio.iscoroutinefunction(watcher):
                    await watcher(old_settings, new_settings)
                else:
                    watcher(old_settings, new_settings)
            except Exception as e:
                logging.error("Configuration watcher error: %s", e)
    
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


# Backward compatibility
def get_settings_compat() -> Settings:
    """Returns current settings (backward compatibility)."""
    return get_settings()


# Convenience functions for common settings
def get_bot_token() -> str:
    """Returns bot token."""
    return config_manager.get_setting('TELEGRAM_BOT_TOKEN')


def get_database_url() -> str:
    """Returns database URL."""
    return config_manager.get_setting('DATABASE_URL')


def get_admin_id() -> int:
    """Returns admin ID."""
    return config_manager.get_setting('ADMIN_ID')


def get_gemini_keys() -> List[str]:
    """Returns Gemini API keys."""
    return config_manager.get_setting('GEMINI_API_KEYS', [])


def get_tavily_keys() -> List[str]:
    """Returns Tavily API keys."""
    return config_manager.get_setting('TAVILY_API_KEYS', [])
