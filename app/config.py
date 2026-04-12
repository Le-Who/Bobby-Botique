import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

# Single source of truth for default Gemini models.
# Referenced by Settings.AVAILABLE_MODELS, Settings.DAILY_LIMITS, and load_settings().
DEFAULT_GEMINI_MODELS: list[str] = [
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
DEFAULT_DAILY_LIMIT_PER_MODEL: int = 15

# --- Imagen 4 model identifiers (AI Studio / Gemini API) ---
IMAGEN_MODEL_FAST: str = "imagen-4.0-fast-generate-001"
IMAGEN_MODEL_BASE: str = "imagen-4.0-generate-001"
IMAGEN_MODEL_ULTRA: str = "imagen-4.0-ultra-generate-001"
IMAGEN_MODELS_ORDERED: list[str] = [IMAGEN_MODEL_FAST, IMAGEN_MODEL_BASE, IMAGEN_MODEL_ULTRA]

# --- Pollinations.ai image models ---
# Overridable via env: IMAGE_MODELS="flux,zimage,gptimage"
# The list determines which buttons appear in the /draw Canvas keyboard.
DEFAULT_POLLINATIONS_IMAGE_MODELS: list[str] = ["flux", "zimage"]
DEFAULT_POLLINATIONS_IMAGE_MODEL: str = "flux"
# Pollinations API base URL
POLLINATIONS_BASE_URL: str = "https://gen.pollinations.ai"


def _load_int_env(env_var_name: str, required: bool = True):
    raw = os.getenv(env_var_name)
    if raw is None or raw == "":
        if required:
            raise ValueError(f"Required environment variable '{env_var_name}' is not set.")
        return None
    cleaned = raw.strip().strip('"').strip("'").strip()
    return int(cleaned)


def _load_and_clean_keys(env_var_name: str, required: bool = True) -> list[str]:
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
            raise ValueError(f"Required environment variable '{env_var_name}' is not set.")
        return []

    # Clean the string from quotes and whitespace, then split.
    cleaned_v = value.strip().strip('"').strip("'")
    keys = [key.strip() for key in cleaned_v.split(",") if key.strip()]
    if required and not keys:
        raise ValueError(f"Environment variable '{env_var_name}' is set but contains no valid keys.")
    return keys


def _load_single_model(env_var_name: str, fallback: str) -> str:
    """
    Робустно загружает имя одной модели из env.
    Если пользователь случайно передал список через запятую, берет первую модель.
    """
    value = os.getenv(env_var_name)
    if not value or not value.strip():
        return fallback

    cleaned_v = value.strip().strip('"').strip("'")
    keys = [key.strip() for key in cleaned_v.split(",") if key.strip()]
    if keys:
        return keys[0]
    return fallback


def _load_daily_limits() -> dict[str, int]:
    """
    Загружает DAILY_LIMITS from env переменной to formatе JSON or компактном формате.

    Формат в env (JSON, рекомендуется):
    DAILY_LIMITS='{"gemini-2.5-flash": 250, "gemini-2.5-flash-lite": 15}'

    Или компактный формат:
    DAILY_LIMITS='gemini-2.5-flash:250,gemini-2.5-flash-lite:15'

    Returns:
        Dict[str, int]: Словарь с limitами for моделей
    """
    value = os.getenv("DAILY_LIMITS")

    # Reuse module-level constant for defaults
    default_limits = dict.fromkeys(DEFAULT_GEMINI_MODELS, DEFAULT_DAILY_LIMIT_PER_MODEL)

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
                raise ValueError("No valid limits found") from None
    except (ValueError, AttributeError, json.JSONDecodeError) as e:
        logging.warning(
            "Failed to parse DAILY_LIMITS from env (raw=%r): %s. Using defaults.",
            value,
            e,
        )
        return default_limits


def get_model_hash(model_name: str) -> str:
    """
    Генерирует короткий хэш models (8 символов) for использования в callback_data.

    Args:
        model_name: Полное имя models

    Returns:
        str: 8-символьный хэш models
    """
    return hashlib.sha256(model_name.encode()).hexdigest()[:8]


# We use BaseModel, NOT BaseSettings. We are not auto-loading from the environment.
class Settings(BaseModel):
    """
    Defines the shape and types of our settings for validation.
    Data is loaded manually and then passed here to be validated.
    """

    # --- CORE ---
    TELEGRAM_BOT_TOKEN: str
    ADMIN_SECRET: str | None = None
    GEMINI_API_KEYS: list[str]
    TAVILY_API_KEYS: list[str]
    OPENROUTER_API_KEYS: list[str] = []  # Optional, by default empty list
    ELEVENLABS_API_KEYS: list[str] = []  # Optional, free-tier ElevenLabs keys
    ELEVENLABS_VOICE_ID: str = "XB0fDUnXU5powFXDhCwa"  # Charlotte — conversational
    # Imagen image generation — uses same GEMINI_API_KEYS pool.
    # A separate per-key RPD counter is maintained in ImagenProvider so that
    # image quota exhaustion does NOT suspend keys for LLM / audio traffic.
    IMAGE_GEN_DAILY_LIMIT: int = 10  # Max image generations per user per day
    IMAGE_GEN_RPD_PER_KEY: int = 25  # Imagen free-tier limit per API key per day
    IMAGE_GEN_TIMEOUT: float = 60.0  # Max seconds to wait for Imagen API response
    IMAGE_GEN_MAX_RETRIES: int = 3  # Key rotation retries on quota/error

    # --- Pollinations.ai image generation ---
    # Models shown as buttons in /draw Canvas. Loaded from IMAGE_MODELS env var.
    POLLINATIONS_IMAGE_MODELS: list[str] = DEFAULT_POLLINATIONS_IMAGE_MODELS.copy()
    POLLINATIONS_DEFAULT_IMAGE_MODEL: str = DEFAULT_POLLINATIONS_IMAGE_MODEL
    # Optional API key. Pollinations also works without a key (rate-limited).
    POLLINATIONS_API_KEY: str = ""
    DATABASE_URL: str
    ADMIN_ID: int
    PORT: int
    ENABLE_WEB_SERVER: bool = True  # Default to True for cloud deployments
    # Base URL of the web server (e.g. https://gemaibotv2-xxxx.northflank.app).
    # Required for Mini App reader links. If empty, system falls back to Telegraph.
    WEBAPP_BASE_URL: str = ""

    # --- LOCAL BOT API SERVER ---
    # If set, the bot routes through a self-hosted Local Bot API Server
    # instead of api.telegram.org and enables local_mode in PTB.
    # Example: "http://tg-api:8081/bot"
    TELEGRAM_LOCAL_SERVER_URL: str = ""

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096

    # --- MODELS ---
    # Модели загружаются from env переменных, значения by default используются if не указаны
    AVAILABLE_MODELS: list[str] = DEFAULT_GEMINI_MODELS.copy()
    DEFAULT_MODEL: str = "gemini-3.1-flash-lite-preview"
    QNA_MODEL: str = "gemini-2.5-flash-lite"
    RESEARCH_MODEL: str = "gemini-3.1-flash-lite-preview"
    URL_SELECTION_MODEL: str = "gemini-3.1-flash-lite-preview"
    TAXONOMY_MODEL: str = "gemini-3.1-flash-lite-preview"  # MemPalace: wing/room classification + contradiction judge

    # --- OPENROUTER MODELS ---
    # Модели загружаются from env переменных, значения by default используются if не указаны
    OPENROUTER_AVAILABLE_MODELS: list[str] = []
    OPENROUTER_DEFAULT_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_QNA_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_RESEARCH_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_URL_SELECTION_MODEL: str = "stepfun/step-3.5-flash:free"

    # --- API PROVIDER SELECTION ---
    USE_OPENROUTER: bool = False  # По умолчанию use Gemini, можно переkeysть на OpenRouter

    # --- LIMITS ---
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97
    TAVILY_QNA_SEARCH_COST: int = 2
    TAVILY_ADVANCED_SEARCH_COST: int = 2
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    # DAILY_LIMITS загружается from env переменной DAILY_LIMITS to formatе JSON
    DAILY_LIMITS: dict[str, int] = dict.fromkeys(DEFAULT_GEMINI_MODELS, DEFAULT_DAILY_LIMIT_PER_MODEL)
    ALERT_COOLDOWN_SECONDS: int = 3600
    MAX_DOCUMENTS_PER_USER: int = 5
    MAX_CONCURRENT_HEAVY_REQUESTS: int = 4
    MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS: int = 1
    LRU_STATE_CACHE_SIZE: int = 1000  # In-memory UserState cap; prevents OOM on free-tier containers

    # --- AGENTIC RESEARCH ---
    JINA_API_KEY: str = ""
    AGENTIC_MAX_ITERATIONS: int = 5
    AGENTIC_MAX_PAGES: int = 3
    AGENTIC_MAX_TOKENS: int = 100_000  # Token budget cap for the entire agentic session
    AGENTIC_TIMEOUT_SECONDS: int = 90  # Wall-clock timeout for the agentic loop
    AGENTIC_MODEL: str = ""  # Defaults to RESEARCH_MODEL if empty
    AGENTIC_PAGE_CONTENT_LIMIT: int = 8192  # Max chars per page (truncation threshold)
    ADAPTIVE_THINKING_ENABLED: bool = True  # Auto-resolve thinking_level when user has no preference
    INLINE_THINKING_LEVEL: str = "low"  # minimal, low, medium, or high

    # --- CONTEXT BUDGETS (per-model effective token limits) ---
    # All Gemini models have 1M token context windows, but reasoning quality
    # degrades significantly beyond ~20% utilization (research: github.com/google-gemini,
    # reddit.com/r/GoogleGeminiAI "context rot" reports, March 2026).
    # flash-lite: lighter architecture, faster degradation → conservative 32K.
    # flash:      good quality up to ~128K (validated sweet spot for reasoning).
    MODEL_CONTEXT_BUDGETS: dict[str, int] = {
        "flash-lite": 32_000,  # gemini-2.5-flash-lite, gemini-3.1-flash-lite-preview
        "flash": 128_000,  # gemini-2.5-flash, gemini-3-flash-preview
    }
    DEFAULT_CONTEXT_BUDGET: int = 128_000

    # --- SAFETY ---
    SAFETY_SETTINGS: list[dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # System prompts are managed by app.prompts.compose_system_instruction()
    # and app.prompt_registry — not duplicated here.


def load_settings() -> Settings:
    """
    Manually loads all settings from the environment and validates them
    using the Pydantic model. This is the most robust method.
    """
    try:
        # Значения by default for моделей
        default_gemini_models = DEFAULT_GEMINI_MODELS.copy()
        default_openrouter_models: list[str] = []

        inline_thinking = os.getenv("INLINE_THINKING_LEVEL", "low").lower()
        if inline_thinking not in ("minimal", "low", "medium", "high"):
            logging.warning("Invalid INLINE_THINKING_LEVEL '%s', falling back to 'low'", inline_thinking)
            inline_thinking = "low"

        # Manually load all values from the environment.
        raw_settings = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "ADMIN_SECRET": os.getenv("ADMIN_SECRET"),
            "DATABASE_URL": os.getenv("DATABASE_URL"),
            "ADMIN_ID": _load_int_env("ADMIN_ID"),
            "PORT": os.getenv("PORT", "10000"),  # Provide a default for PORT
            "ENABLE_WEB_SERVER": os.getenv("ENABLE_WEB_SERVER", "true").lower() == "true",
            "WEBAPP_BASE_URL": os.getenv("WEBAPP_BASE_URL", "").rstrip("/"),
            "TELEGRAM_LOCAL_SERVER_URL": os.getenv("TELEGRAM_LOCAL_SERVER_URL", "").rstrip("/"),
            "GEMINI_API_KEYS": _load_and_clean_keys("GEMINI_API_KEYS"),
            "TAVILY_API_KEYS": _load_and_clean_keys("TAVILY_API_KEYS"),
            "OPENROUTER_API_KEYS": _load_and_clean_keys("OPENROUTER_API_KEYS", required=False),
            "ELEVENLABS_API_KEYS": _load_and_clean_keys("ELEVENLABS_API_KEYS", required=False),
            "ELEVENLABS_VOICE_ID": os.getenv("ELEVENLABS_VOICE_ID", "XB0fDUnXU5powFXDhCwa"),
            # Pollinations image generation
            "POLLINATIONS_IMAGE_MODELS": _load_and_clean_keys("IMAGE_MODELS", required=False)
            or DEFAULT_POLLINATIONS_IMAGE_MODELS.copy(),
            "POLLINATIONS_DEFAULT_IMAGE_MODEL": os.getenv("DEFAULT_IMAGE_MODEL", DEFAULT_POLLINATIONS_IMAGE_MODEL),
            "POLLINATIONS_API_KEY": os.getenv("POLLINATIONS_API_KEY", ""),
            # Load models from env or use значения by default
            "AVAILABLE_MODELS": _load_and_clean_keys("GEMINI_AVAILABLE_MODELS", required=False)
            or default_gemini_models,
            "OPENROUTER_AVAILABLE_MODELS": _load_and_clean_keys("OPENROUTER_AVAILABLE_MODELS", required=False)
            or default_openrouter_models,
            "DEFAULT_MODEL": _load_single_model("DEFAULT_MODEL", "gemini-3.1-flash-lite-preview"),
            "QNA_MODEL": _load_single_model("QNA_MODEL", "gemini-2.5-flash-lite"),
            "RESEARCH_MODEL": _load_single_model("RESEARCH_MODEL", "gemini-3.1-flash-lite-preview"),
            "URL_SELECTION_MODEL": _load_single_model("URL_SELECTION_MODEL", "gemini-3.1-flash-lite-preview"),
            "TAXONOMY_MODEL": _load_single_model("TAXONOMY_MODEL", "gemini-3.1-flash-lite-preview"),
            "OPENROUTER_DEFAULT_MODEL": _load_single_model("OPENROUTER_DEFAULT_MODEL", "stepfun/step-3.5-flash:free"),
            "OPENROUTER_QNA_MODEL": _load_single_model("OPENROUTER_QNA_MODEL", "stepfun/step-3.5-flash:free"),
            "OPENROUTER_RESEARCH_MODEL": _load_single_model("OPENROUTER_RESEARCH_MODEL", "stepfun/step-3.5-flash:free"),
            "OPENROUTER_URL_SELECTION_MODEL": _load_single_model(
                "OPENROUTER_URL_SELECTION_MODEL", "stepfun/step-3.5-flash:free"
            ),
            "DAILY_LIMITS": _load_daily_limits(),
            "MAX_CONCURRENT_HEAVY_REQUESTS": int(os.getenv("MAX_CONCURRENT_HEAVY_REQUESTS", "4")),
            "MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS": int(os.getenv("MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS", "1")),
            "LRU_STATE_CACHE_SIZE": int(os.getenv("LRU_STATE_CACHE_SIZE", "1000")),
            "JINA_API_KEY": os.getenv("JINA_API_KEY", ""),
            "AGENTIC_MAX_ITERATIONS": int(os.getenv("AGENTIC_MAX_ITERATIONS", "5")),
            "AGENTIC_MAX_PAGES": int(os.getenv("AGENTIC_MAX_PAGES", "3")),
            "AGENTIC_MAX_TOKENS": int(os.getenv("AGENTIC_MAX_TOKENS", "100000")),
            "AGENTIC_TIMEOUT_SECONDS": int(os.getenv("AGENTIC_TIMEOUT_SECONDS", "90")),
            "AGENTIC_MODEL": os.getenv("AGENTIC_MODEL", ""),
            "AGENTIC_PAGE_CONTENT_LIMIT": int(os.getenv("AGENTIC_PAGE_CONTENT_LIMIT", "8192")),
            "ADAPTIVE_THINKING_ENABLED": os.getenv("ADAPTIVE_THINKING_ENABLED", "true").lower() == "true",
            "INLINE_THINKING_LEVEL": inline_thinking,
        }

        # Validation: проверяем, что DEFAULT_MODEL и другие константы есть в списках моделей
        settings_obj = Settings(**raw_settings)

        # Check Gemini models
        if settings_obj.DEFAULT_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                "DEFAULT_MODEL '%s' not in AVAILABLE_MODELS. Adding it.",
                settings_obj.DEFAULT_MODEL,
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.DEFAULT_MODEL)

        if settings_obj.QNA_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                "QNA_MODEL '%s' not in AVAILABLE_MODELS. Adding it.",
                settings_obj.QNA_MODEL,
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.QNA_MODEL)

        if settings_obj.RESEARCH_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                "RESEARCH_MODEL '%s' not in AVAILABLE_MODELS. Adding it.",
                settings_obj.RESEARCH_MODEL,
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.RESEARCH_MODEL)

        # Check OpenRouter models
        if settings_obj.OPENROUTER_DEFAULT_MODEL not in settings_obj.OPENROUTER_AVAILABLE_MODELS:
            logging.warning(
                f"OPENROUTER_DEFAULT_MODEL '{settings_obj.OPENROUTER_DEFAULT_MODEL}' not in OPENROUTER_AVAILABLE_MODELS. Adding it."
            )
            settings_obj.OPENROUTER_AVAILABLE_MODELS.append(settings_obj.OPENROUTER_DEFAULT_MODEL)

        return settings_obj
    except (ValidationError, ValueError) as e:
        # Catch errors from both Pydantic and our manual functions.
        # Catch errors from both Pydantic and our manual functions.
        error_msg = f"FATAL: Could not load settings. Please check your environment variables. Error: {e}"
        raise ValueError(error_msg) from e


# --- TIMEZONES ---
# Кэшируем временные зоны for предотвращения requestов к pg_timezone_names
PACIFIC_TZ = ZoneInfo("US/Pacific")
KYIV_TZ = ZoneInfo("Europe/Kyiv")
UTC_TZ = UTC

# --- LAZY LOADING SETTINGS ---
_settings_instance: Settings | None = None


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
def get_settings_safe() -> Settings | None:
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
    settings = None  # type: ignore[assignment]  # Settings is expected, None only in dev


class ConfigManager:
    """Manages configuration with hot reloading capability."""

    def __init__(self):
        self._settings = get_settings_safe()
        self._last_reload = time.time()
        self._reload_interval = 300  # 5 minutes
        self._watchers: list[Callable] = []
        self._lock = asyncio.Lock()
        self._reload_task: asyncio.Task | None = None

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
        """Reloads configuration from environment."""
        async with self._lock:
            try:
                new_settings = load_settings()  # bypass singleton cache — get_settings() returns stale instance

                # Check if any critical settings changed
                critical_changed = (
                    new_settings.TELEGRAM_BOT_TOKEN != self._settings.TELEGRAM_BOT_TOKEN
                    or new_settings.DATABASE_URL != self._settings.DATABASE_URL
                    or new_settings.ADMIN_ID != self._settings.ADMIN_ID
                )

                if critical_changed:
                    logging.warning("Critical configuration changed, restart may be required")

                # Validate DEFAULT_MODEL exists in available models
                all_available_models = set()
                if new_settings.AVAILABLE_MODELS:
                    all_available_models.update(new_settings.AVAILABLE_MODELS)
                if new_settings.OPENROUTER_AVAILABLE_MODELS:
                    all_available_models.update(new_settings.OPENROUTER_AVAILABLE_MODELS)

                if new_settings.DEFAULT_MODEL not in all_available_models:
                    logging.error(
                        "DEFAULT_MODEL '%s' not in AVAILABLE_MODELS!",
                        new_settings.DEFAULT_MODEL,
                    )
                    raise ValueError("DEFAULT_MODEL must be in AVAILABLE_MODELS")

                # Update settings
                old_settings = self._settings
                self._settings = new_settings
                self._last_reload = time.time()

                # Notify watchers (including model migration)
                await self._notify_watchers(old_settings, new_settings)

                logging.info("Configuration reloaded successfully.")

            except Exception as e:
                logging.error("Failed to reload configuration: %s", e, exc_info=True)
                # Keep running with old settings

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


def get_gemini_keys() -> list[str]:
    """Returns Gemini API keys."""
    return config_manager.get_setting("GEMINI_API_KEYS", [])


def get_tavily_keys() -> list[str]:
    """Returns Tavily API keys."""
    return config_manager.get_setting("TAVILY_API_KEYS", [])


def get_openrouter_keys() -> list[str]:
    """Returns OpenRouter API keys."""
    return config_manager.get_setting("OPENROUTER_API_KEYS", [])


def get_use_openrouter() -> bool:
    """Returns whether to use OpenRouter instead of Gemini."""
    return config_manager.get_setting("USE_OPENROUTER", False)
