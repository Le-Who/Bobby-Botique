import os
import pytz
import time
import asyncio
import logging
from typing import List, Dict, Callable, Any
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
    DEFAULT_SYSTEM_PROMPT: str = """Ты - полезный AI ассистент. Отвечай на вопросы пользователя, используя правильное форматирование для Telegram.

**ПРАВИЛА ФОРМАТИРОВАНИЯ:**
1. Используй Telegram MarkdownV2 синтаксис:
   - Для жирного текста: `*жирный текст*` (НЕ `**жирный текст**`)
   - Для курсива: `_курсив_` (НЕ `__курсив__`)
   - Для кода: `` `код` ``
   - Для списков: каждый элемент начинается с `- `

2. **КРИТИЧЕСКИЕ ПРАВИЛА:**
   - НИКОГДА не используй HTML теги: `<b>`, `<i>`, `<code>`, `<a>`, etc.
   - НИКОГДА не используй двойные звездочки `**текст**` - используй одинарные `*текст*`
   - НИКОГДА не используй двойные подчеркивания `__текст__` - используй одинарные `_текст_`
   - НИКОГДА не используй LaTeX математические выражения: `$...$` или `$$...$$`

3. **МАТЕМАТИЧЕСКИЕ ВЫРАЖЕНИЯ:**
   - НИКОГДА не используй LaTeX: `$1 \\times 1 = 1$` или `$$\\sqrt{2}$$`
   - ВСЕГДА используй обычный текст: `1 × 1 = 1` или `√2` или `корень из 2`
   - Для дробей: используй `/` (например, `1/2` вместо `$\\frac{1}{2}$`)
   - Для корней: используй `√` или `корень из` (например, `√2` или `корень из 2`)
   - Для степеней: используй `^` (например, `2^2 = 4` вместо `$2^2 = 4$`)
   - Для умножения: используй `×` или `*` (например, `2 × 3 = 6` или `2 * 3 = 6`)

Будь полезным, точным и дружелюбным в своих ответах."""

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
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
# Create the one and only settings object for the app.
settings = load_settings()


class ConfigManager:
    """Manages configuration with hot reloading capability."""
    
    def __init__(self):
        self._settings = settings
        self._last_reload = time.time()
        self._reload_interval = 300  # 5 minutes
        self._watchers: List[Callable] = []
        self._lock = asyncio.Lock()
    
    @property
    def settings(self) -> Settings:
        """Returns current settings, reloading if necessary."""
        current_time = time.time()
        if current_time - self._last_reload > self._reload_interval:
            asyncio.create_task(self._reload_config())
        return self._settings
    
    async def _reload_config(self) -> None:
        """Reloads configuration from environment."""
        async with self._lock:
            try:
                new_settings = load_settings()
                
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
def get_settings() -> Settings:
    """Returns current settings (backward compatibility)."""
    return config_manager.settings


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
