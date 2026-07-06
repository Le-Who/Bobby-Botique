import asyncio
import functools
import hashlib
import inspect
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from app.utils.json_compat import json

# Single source of truth for default Gemini models.
# Referenced by Settings.AVAILABLE_MODELS, Settings.DAILY_LIMITS, and load_settings().
GEMINI_PRIMARY_MODEL: str = "gemini-3.5-flash"
GEMINI_ECONOMY_MODEL: str = "gemini-3.1-flash-lite"
DEFAULT_GEMINI_MODELS: list[str] = [
    GEMINI_PRIMARY_MODEL,
    GEMINI_ECONOMY_MODEL,
]
CURRENT_GEMINI_MODELS: tuple[str, ...] = (GEMINI_PRIMARY_MODEL, GEMINI_ECONOMY_MODEL)
DEFAULT_DAILY_LIMIT_PER_MODEL: int = 15
DEFAULT_DAILY_LIMITS_BY_MODEL: dict[str, int] = {
    GEMINI_PRIMARY_MODEL: DEFAULT_DAILY_LIMIT_PER_MODEL,
    GEMINI_ECONOMY_MODEL: 400,
}

# --- Imagen 4 model identifiers (AI Studio / Gemini API) ---
IMAGEN_MODEL_FAST: str = "imagen-4.0-fast-generate-001"
IMAGEN_MODEL_BASE: str = "imagen-4.0-generate-001"
IMAGEN_MODEL_ULTRA: str = "imagen-4.0-ultra-generate-001"
IMAGEN_MODELS_ORDERED: list[str] = [IMAGEN_MODEL_FAST, IMAGEN_MODEL_BASE, IMAGEN_MODEL_ULTRA]

# --- Gemini Live API (real-time bidirectional audio) ---
# The current deployed live path in this repo uses the Gemini GenAI Live API.
GEMINI_LIVE_MODEL: str = "gemini-3.1-flash-live-preview"
GEMINI_LIVE_VOICE_NAME: str = os.getenv("GEMINI_LIVE_VOICE_NAME", "Aoede").strip() or "Aoede"

# --- Pollinations.ai image models ---
# Overridable via env: IMAGE_MODELS="flux,zimage,gptimage,..."
# The list determines which buttons appear in the /draw Canvas keyboard AND
# which models are accepted by PollinationsProvider.generate() without
# falling back.  Includes all models advertised in the inline handler.
DEFAULT_POLLINATIONS_IMAGE_MODELS: list[str] = [
    "flux",
    "zimage",
    "gptimage-1-5",
    "gptimage",
    "qwen-image",
    "wan-image",
    "klein",
]
DEFAULT_POLLINATIONS_IMAGE_MODEL: str = "zimage"
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


def _filter_current_gemini_models(models: list[str], *, include_defaults: bool = True) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    allowed = set(CURRENT_GEMINI_MODELS)
    for model in models:
        normalized = model.strip()
        if normalized in allowed and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    if include_defaults:
        for model in CURRENT_GEMINI_MODELS:
            if model not in seen:
                result.append(model)
                seen.add(model)
    return result


def normalize_gemini_chat_model(model_name: str | None, fallback: str = GEMINI_PRIMARY_MODEL) -> str:
    if isinstance(model_name, str) and model_name.strip() in CURRENT_GEMINI_MODELS:
        return model_name.strip()
    return fallback


def _load_gemini_role_model(env_var_name: str, fallback: str) -> str:
    return normalize_gemini_chat_model(_load_single_model(env_var_name, fallback), fallback=fallback)


def _load_daily_limits() -> dict[str, int]:
    """
    Загружает DAILY_LIMITS from env переменной to formatе JSON or компактном формате.

    Формат в env (JSON, рекомендуется):
    DAILY_LIMITS='{"gemini-3.5-flash": 15, "gemini-3.1-flash-lite": 400}'

    Или компактный формат:
    DAILY_LIMITS='gemini-3.5-flash:15,gemini-3.1-flash-lite:400'

    Returns:
        Dict[str, int]: Словарь с limitами for моделей
    """
    value = os.getenv("DAILY_LIMITS")

    # Reuse module-level constant for defaults
    default_limits = {
        model: DEFAULT_DAILY_LIMITS_BY_MODEL.get(model, DEFAULT_DAILY_LIMIT_PER_MODEL)
        for model in DEFAULT_GEMINI_MODELS
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
                raise ValueError("No valid limits found") from None
    except (ValueError, AttributeError, json.JSONDecodeError) as e:
        logging.warning(
            "Failed to parse DAILY_LIMITS from env (raw=%r): %s. Using defaults.",
            value,
            e,
        )
        return default_limits


@functools.lru_cache(maxsize=32)
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
    ELEVENLABS_MODEL: str = "eleven_multilingual_v2"
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

    # --- Vertex AI Express (optional resilience pathway for judge) ---
    # Provides a stable alternative endpoint for gemini-3.1-flash-lite
    # when the Gemini API is under high load (503 storms).
    # Requires a *Google Cloud* API key — NOT a Gemini AI Studio key.
    # How to get one: GCP Console → APIs & Services → Credentials → Create API Key
    # (or use Vertex AI Express Mode for a free-tier key).
    VERTEX_AI_KEY: str = ""  # GCP API key bound to a service account
    VERTEX_AI_PROJECT: str = ""  # GCP project ID where Vertex AI API is enabled
    VERTEX_AI_LOCATION: str = "us-central1"  # Vertex AI region

    # --- Weather & Currency Direct APIs ---
    # WeatherAPI.com free tier: 1M req/month. https://www.weatherapi.com/
    # If empty, intent_router falls back to Open-Meteo (2 requests instead of 1).
    WEATHER_API_KEY: str = ""
    # ExchangeRate-API free tier: 1,500 req/month. https://www.exchangerate-api.com/
    # If empty, intent_router falls back to Frankfurter (no RUB support).
    EXCHANGE_RATE_API_KEY: str = ""
    DATABASE_URL: str
    ADMIN_ID: int
    PORT: int
    ENABLE_WEB_SERVER: bool = True  # Default to True for cloud deployments
    WEBHOOK_SECRET_TOKEN: str = ""
    WEBHOOK_MAX_CONNECTIONS: int = 40
    UPDATE_QUEUE_MAXSIZE: int = 1000
    # Base URL of the web server (e.g. https://bot.example.com).
    # Required for Mini App reader links. If empty, system falls back to Telegraph.
    WEBAPP_BASE_URL: str = ""
    # Short name of the Mini App registered with @BotFather via /newapp.
    # When set, Crocodile game buttons use t.me deep links (no "Open link?" dialog).
    # Example: if short name is "game", button URL becomes https://t.me/{bot}/game?startapp={id}
    MINIAPP_SHORT_NAME: str = ""
    # Optional external game hub Mini App hosted by the CC-GH project.
    # This intentionally stays separate from MINIAPP_SHORT_NAME, which belongs to Crocodile.
    GAME_HUB_URL: str = ""
    GAME_HUB_DIRECT_LINK: str = ""
    GAME_HUB_MINIAPP_SHORT_NAME: str = "games"

    # --- NATAL CHART REPORTS ---
    NATAL_REPORTS_ENABLED: bool = False
    NATAL_REPORT_TTL_DAYS: int = 365
    NATAL_GEOCODER_PROVIDER: str = "local"
    NATAL_CITY_OVERRIDES_PATH: str = ""
    NATAL_SEND_RAW_BIRTH_DATA_TO_LLM: bool = False

    # --- LOCAL BOT API SERVER ---
    # If set, the bot routes through a self-hosted Local Bot API Server
    # instead of api.telegram.org and enables local_mode in PTB.
    # Example: "http://tg-api:8081/bot"
    TELEGRAM_LOCAL_SERVER_URL: str = ""

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096
    TAROT_IDLE_CONFIRM_AFTER_SECONDS: int = 86_400

    # --- MODELS ---
    # Модели загружаются from env переменных, значения by default используются if не указаны
    AVAILABLE_MODELS: list[str] = DEFAULT_GEMINI_MODELS.copy()
    DEFAULT_MODEL: str = GEMINI_PRIMARY_MODEL
    QNA_MODEL: str = GEMINI_ECONOMY_MODEL
    INLINE_MODEL: str = GEMINI_ECONOMY_MODEL
    RESEARCH_MODEL: str = GEMINI_PRIMARY_MODEL
    URL_SELECTION_MODEL: str = GEMINI_ECONOMY_MODEL
    TAXONOMY_MODEL: str = GEMINI_ECONOMY_MODEL  # MemPalace: wing/room classification + contradiction judge

    # --- OPENROUTER MODELS ---
    # Модели загружаются from env переменных, значения by default используются if не указаны
    OPENROUTER_AVAILABLE_MODELS: list[str] = []
    OPENROUTER_DEFAULT_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_QNA_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_RESEARCH_MODEL: str = "stepfun/step-3.5-flash:free"
    OPENROUTER_URL_SELECTION_MODEL: str = "stepfun/step-3.5-flash:free"

    # --- OPENCODE GO MODELS ---
    # Full model list as of 2026-05-01 (opencode.ai/docs/go):
    #   glm-5, glm-5.1, kimi-k2.5, kimi-k2.6, mimo-v2-pro, mimo-v2-omni,
    #   mimo-v2.5-pro, mimo-v2.5, minimax-m2.5, minimax-m2.7,
    #   qwen3.5-plus, qwen3.6-plus, deepseek-v4-pro, deepseek-v4-flash
    # All use prefix opencode-go/<model-id>.
    # Support comma-separated key list for rotation (same pattern as GEMINI_API_KEYS).
    OPENCODE_API_KEYS: list[str] = []  # sk-... keys, rotatable
    OPENCODE_AVAILABLE_MODELS: list[str] = []  # populated from env OPENCODE_AVAILABLE_MODELS
    OPENCODE_DEFAULT_MODEL: str = "opencode-go/deepseek-v4-flash"
    OPENCODE_QNA_MODEL: str = "opencode-go/qwen3.6-plus"  # High quality dialog
    OPENCODE_RESEARCH_MODEL: str = "opencode-go/deepseek-v4-pro"  # Deep reasoning
    OPENCODE_URL_SELECTION_MODEL: str = "opencode-go/big-pickle"
    OPENCODE_VISION_MODEL: str = "opencode-go/mimo-v2-omni"  # Multimodal
    OPENCODE_INLINE_MODEL: str = "opencode-go/deepseek-v4-flash"  # Fast but pleasant

    # --- FREETHEAI MODELS ---
    # FreeTheAI router (freetheai.xyz/docs): supports chat (cat/, yng/),
    # image (vhr/), and audio (or/google/lyria-*) models.
    # Keys are Bearer tokens, comma-separated for rotation.
    FREETHEAI_API_KEYS: list[str] = []
    FREETHEAI_AVAILABLE_MODELS: list[str] = []
    FREETHEAI_DEFAULT_MODEL: str = "cat/claude-4-6-sonnet"

    # --- API PROVIDER SELECTION ---
    # "opencode" routes primary chat/search/inline through Opencode Go with Gemini fallback.
    # "gemini"   bypasses Opencode Go entirely (admin toggle via /set_provider).
    PRIMARY_PROVIDER: str = "opencode"  # "opencode" | "gemini"
    USE_OPENROUTER: bool = False  # По умолчанию use Gemini, можно переключить на OpenRouter

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
    LRU_STATE_CACHE_SIZE: int = 1000  # In-memory UserState cap

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
        "flash-lite": 32_000,  # gemini-3.1-flash-lite
        "flash": 128_000,  # gemini-3.5-flash
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
        configured_gemini_models = _filter_current_gemini_models(
            _load_and_clean_keys("GEMINI_AVAILABLE_MODELS", required=False) or default_gemini_models
        )

        inline_thinking = os.getenv("INLINE_THINKING_LEVEL", "").strip().lower()
        if not inline_thinking:
            inline_thinking = "low"
        elif inline_thinking not in ("minimal", "low", "medium", "high"):
            logging.warning("Invalid INLINE_THINKING_LEVEL '%s', falling back to 'low'", inline_thinking)
            inline_thinking = "low"

        # Manually load all values from the environment.
        raw_settings = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "ADMIN_SECRET": (os.getenv("ADMIN_SECRET") or "").strip().strip("\"'").strip() or None,
            "DATABASE_URL": os.getenv("DATABASE_URL"),
            "ADMIN_ID": _load_int_env("ADMIN_ID", required=False) or 0,
            "PORT": os.getenv("PORT", "10000"),  # Provide a default for PORT
            "ENABLE_WEB_SERVER": os.getenv("ENABLE_WEB_SERVER", "true").lower() == "true",
            "WEBHOOK_SECRET_TOKEN": os.getenv("WEBHOOK_SECRET_TOKEN", "").strip(),
            "WEBHOOK_MAX_CONNECTIONS": int(os.getenv("WEBHOOK_MAX_CONNECTIONS", "40")),
            "UPDATE_QUEUE_MAXSIZE": int(os.getenv("UPDATE_QUEUE_MAXSIZE", "1000")),
            "WEBAPP_BASE_URL": os.getenv("WEBAPP_BASE_URL", "").rstrip("/"),
            "MINIAPP_SHORT_NAME": os.getenv("MINIAPP_SHORT_NAME", "").strip(),
            "GAME_HUB_URL": os.getenv("GAME_HUB_URL", "").rstrip("/"),
            "GAME_HUB_DIRECT_LINK": os.getenv("GAME_HUB_DIRECT_LINK", "").strip(),
            "GAME_HUB_MINIAPP_SHORT_NAME": os.getenv("GAME_HUB_MINIAPP_SHORT_NAME", "games").strip(),
            "NATAL_REPORTS_ENABLED": os.getenv("NATAL_REPORTS_ENABLED", "false").lower() == "true",
            "NATAL_REPORT_TTL_DAYS": int(os.getenv("NATAL_REPORT_TTL_DAYS", "365")),
            "NATAL_GEOCODER_PROVIDER": os.getenv("NATAL_GEOCODER_PROVIDER", "local").strip().lower(),
            "NATAL_CITY_OVERRIDES_PATH": os.getenv("NATAL_CITY_OVERRIDES_PATH", "").strip(),
            "NATAL_SEND_RAW_BIRTH_DATA_TO_LLM": os.getenv("NATAL_SEND_RAW_BIRTH_DATA_TO_LLM", "false").lower()
            == "true",
            "TELEGRAM_LOCAL_SERVER_URL": os.getenv("TELEGRAM_LOCAL_SERVER_URL", "").rstrip("/"),
            "TAROT_IDLE_CONFIRM_AFTER_SECONDS": int(os.getenv("TAROT_IDLE_CONFIRM_AFTER_SECONDS", "86400")),
            "GEMINI_API_KEYS": _load_and_clean_keys("GEMINI_API_KEYS"),
            "TAVILY_API_KEYS": _load_and_clean_keys("TAVILY_API_KEYS"),
            "OPENROUTER_API_KEYS": _load_and_clean_keys("OPENROUTER_API_KEYS", required=False),
            "ELEVENLABS_API_KEYS": _load_and_clean_keys("ELEVENLABS_API_KEYS", required=False),
            "ELEVENLABS_VOICE_ID": os.getenv("ELEVENLABS_VOICE_ID", "XB0fDUnXU5powFXDhCwa"),
            "ELEVENLABS_MODEL": os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2"),
            # Pollinations image generation
            "POLLINATIONS_IMAGE_MODELS": _load_and_clean_keys("IMAGE_MODELS", required=False)
            or DEFAULT_POLLINATIONS_IMAGE_MODELS.copy(),
            "POLLINATIONS_DEFAULT_IMAGE_MODEL": os.getenv("DEFAULT_IMAGE_MODEL", DEFAULT_POLLINATIONS_IMAGE_MODEL),
            "POLLINATIONS_API_KEY": os.getenv("POLLINATIONS_API_KEY", ""),
            # Vertex AI Express (optional resilience for judge)
            "VERTEX_AI_KEY": os.getenv("VERTEX_AI_KEY", "").strip(),
            "VERTEX_AI_PROJECT": os.getenv("VERTEX_AI_PROJECT", "").strip(),
            "VERTEX_AI_LOCATION": os.getenv("VERTEX_AI_LOCATION", "us-central1").strip(),
            "WEATHER_API_KEY": os.getenv("WEATHER_API_KEY", "").strip(),
            "EXCHANGE_RATE_API_KEY": os.getenv("EXCHANGE_RATE_API_KEY", "").strip(),
            # Load models from env or use значения by default
            "AVAILABLE_MODELS": configured_gemini_models,
            "OPENROUTER_AVAILABLE_MODELS": _load_and_clean_keys("OPENROUTER_AVAILABLE_MODELS", required=False)
            or default_openrouter_models,
            "DEFAULT_MODEL": _load_gemini_role_model("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
            "QNA_MODEL": _load_gemini_role_model("QNA_MODEL", GEMINI_ECONOMY_MODEL),
            "INLINE_MODEL": _load_gemini_role_model("INLINE_MODEL", GEMINI_ECONOMY_MODEL),
            "RESEARCH_MODEL": _load_gemini_role_model("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
            "URL_SELECTION_MODEL": _load_gemini_role_model("URL_SELECTION_MODEL", GEMINI_ECONOMY_MODEL),
            "TAXONOMY_MODEL": _load_gemini_role_model("TAXONOMY_MODEL", GEMINI_ECONOMY_MODEL),
            "OPENROUTER_DEFAULT_MODEL": _load_single_model("OPENROUTER_DEFAULT_MODEL", "stepfun/step-3.5-flash:free"),
            "OPENROUTER_QNA_MODEL": _load_single_model("OPENROUTER_QNA_MODEL", "stepfun/step-3.5-flash:free"),
            "OPENROUTER_RESEARCH_MODEL": _load_single_model("OPENROUTER_RESEARCH_MODEL", "stepfun/step-3.5-flash:free"),
            "OPENROUTER_URL_SELECTION_MODEL": _load_single_model(
                "OPENROUTER_URL_SELECTION_MODEL", "stepfun/step-3.5-flash:free"
            ),
            # Opencode Go provider
            "OPENCODE_API_KEYS": _load_and_clean_keys("OPENCODE_API_KEYS", required=False),
            "OPENCODE_AVAILABLE_MODELS": _load_and_clean_keys("OPENCODE_AVAILABLE_MODELS", required=False),
            "OPENCODE_DEFAULT_MODEL": _load_single_model("OPENCODE_DEFAULT_MODEL", "opencode-go/qwen3.5-plus"),
            "OPENCODE_QNA_MODEL": _load_single_model("OPENCODE_QNA_MODEL", "opencode-go/qwen3.6-plus"),
            "OPENCODE_RESEARCH_MODEL": _load_single_model("OPENCODE_RESEARCH_MODEL", "opencode-go/glm-5.1"),
            "OPENCODE_URL_SELECTION_MODEL": _load_single_model(
                "OPENCODE_URL_SELECTION_MODEL", "opencode-go/big-pickle"
            ),
            "OPENCODE_VISION_MODEL": _load_single_model("OPENCODE_VISION_MODEL", "opencode-go/mimo-v2-omni"),
            "OPENCODE_INLINE_MODEL": _load_single_model("OPENCODE_INLINE_MODEL", "opencode-go/minimax-m2.5"),
            # FreeTheAI provider
            "FREETHEAI_API_KEYS": _load_and_clean_keys("FREETHEAI_API_KEYS", required=False),
            "FREETHEAI_AVAILABLE_MODELS": _load_and_clean_keys("FREETHEAI_AVAILABLE_MODELS", required=False),
            "FREETHEAI_DEFAULT_MODEL": _load_single_model("FREETHEAI_DEFAULT_MODEL", "cat/claude-4-6-sonnet"),
            "PRIMARY_PROVIDER": os.getenv("PRIMARY_PROVIDER", "opencode").strip().lower(),
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
        settings_obj = Settings(**raw_settings)  # type: ignore[arg-type]

        # Check Gemini models
        if settings_obj.DEFAULT_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.warning(
                "DEFAULT_MODEL '%s' not in AVAILABLE_MODELS. Adding it.",
                settings_obj.DEFAULT_MODEL,
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.DEFAULT_MODEL)

        if settings_obj.QNA_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.info(
                "QNA_MODEL '%s' not in AVAILABLE_MODELS. Adding it.",
                settings_obj.QNA_MODEL,
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.QNA_MODEL)

        if settings_obj.INLINE_MODEL not in settings_obj.AVAILABLE_MODELS:
            logging.info(
                "INLINE_MODEL '%s' not in AVAILABLE_MODELS. Adding it.",
                settings_obj.INLINE_MODEL,
            )
            settings_obj.AVAILABLE_MODELS.append(settings_obj.INLINE_MODEL)

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

        # Check Opencode Go models — auto-populate list from role models
        for role_model in (
            settings_obj.OPENCODE_DEFAULT_MODEL,
            settings_obj.OPENCODE_QNA_MODEL,
            settings_obj.OPENCODE_RESEARCH_MODEL,
            settings_obj.OPENCODE_VISION_MODEL,
            settings_obj.OPENCODE_INLINE_MODEL,
        ):
            if role_model and role_model not in settings_obj.OPENCODE_AVAILABLE_MODELS:
                settings_obj.OPENCODE_AVAILABLE_MODELS.append(role_model)

        # Check FreeTheAI models
        # Filter out image models so they only appear in /draw, not in chat menus.
        settings_obj.FREETHEAI_AVAILABLE_MODELS = [
            m for m in settings_obj.FREETHEAI_AVAILABLE_MODELS
            if not m.startswith(("vhr/", "img/"))
        ]
        
        if settings_obj.FREETHEAI_DEFAULT_MODEL not in settings_obj.FREETHEAI_AVAILABLE_MODELS:
            logging.warning(
                f"FREETHEAI_DEFAULT_MODEL '{settings_obj.FREETHEAI_DEFAULT_MODEL}' not in FREETHEAI_AVAILABLE_MODELS. Adding it."
            )
            settings_obj.FREETHEAI_AVAILABLE_MODELS.append(settings_obj.FREETHEAI_DEFAULT_MODEL)

        # Validate PRIMARY_PROVIDER
        if settings_obj.PRIMARY_PROVIDER not in ("opencode", "gemini", "openrouter", "freetheai"):
            logging.warning(
                "Invalid PRIMARY_PROVIDER '%s'. Falling back to 'opencode'.",
                settings_obj.PRIMARY_PROVIDER,
            )
            settings_obj.PRIMARY_PROVIDER = "opencode"

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
    global _settings_instance
    if _settings_instance is not None:
        return _settings_instance
    try:
        _settings_instance = load_settings()
        return _settings_instance
    except Exception:
        return None


# --- SINGLETON INSTANCE ---
# Create the one and only settings object for the app.
# Use lazy loading to prevent import errors
settings: Settings = get_settings_safe()  # type: ignore


class ConfigManager:
    """Manages configuration with hot reloading capability."""

    def __init__(self) -> None:
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

                if self._settings is None:
                    self._settings = new_settings
                    self._last_reload = time.time()
                    logging.info("Configuration loaded initially via reload.")
                    return

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
                if inspect.iscoroutinefunction(watcher):
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


def get_opencode_keys() -> list[str]:
    """Returns Opencode Go API keys."""
    return config_manager.get_setting("OPENCODE_API_KEYS", [])


def get_freetheai_keys() -> list[str]:
    """Returns FreeTheAI API keys."""
    return config_manager.get_setting("FREETHEAI_API_KEYS", [])


def get_all_available_models() -> list[str]:
    """Return the union of all provider model lists.

    Single source of truth — use this instead of manually concatenating
    AVAILABLE_MODELS + OPENROUTER_AVAILABLE_MODELS + ... everywhere.
    Prevents the class of bugs where a new provider's models are added
    to some whitelists but missed in others.
    """
    s = config_manager.settings
    models: list[str] = list(s.AVAILABLE_MODELS or [])
    if s.OPENROUTER_AVAILABLE_MODELS:
        models.extend(s.OPENROUTER_AVAILABLE_MODELS)
    if s.OPENCODE_AVAILABLE_MODELS:
        models.extend(s.OPENCODE_AVAILABLE_MODELS)
    if s.FREETHEAI_AVAILABLE_MODELS:
        models.extend(s.FREETHEAI_AVAILABLE_MODELS)
    return models


# ── Primary provider: DB-backed runtime toggle ────────────────────────────────
# The DB global_settings store is the source-of-truth when the admin uses
# /set_provider.  If the DB is unavailable (e.g. startup), we fall back to
# the env-derived Settings value.

_primary_provider_cache: str | None = None  # simple in-process cache


def _invalidate_primary_provider_cache() -> None:
    """Clear the in-process primary-provider cache (called after /set_provider)."""
    global _primary_provider_cache
    _primary_provider_cache = None


def get_primary_provider() -> str:
    """Returns the currently active primary provider name.

    Reads from (in priority order):
    1. In-process cache (cleared by ``_invalidate_primary_provider_cache``).
    2. DB global_settings key ``primary_provider`` (set by ``/set_provider``).
    3. ``settings.PRIMARY_PROVIDER`` (from env, default ``"opencode"``).

    Returns:
        ``"opencode"`` | ``"gemini"`` | ``"openrouter"``
    """
    global _primary_provider_cache
    if _primary_provider_cache is not None:
        return _primary_provider_cache

    # Try DB (async, so we use a fire-and-forget cache fill here)
    # For synchronous callers the env fallback is used on first call;
    # the DB value is fetched asynchronously the first time through the
    # admin command and then cached.
    try:
        env_value: str = config_manager.get_setting("PRIMARY_PROVIDER", "opencode")
    except Exception:
        env_value = os.getenv("PRIMARY_PROVIDER", "opencode").strip().lower() or "opencode"
    return env_value


async def get_primary_provider_async() -> str:
    """Async version: reads DB first, then env fallback, updates in-process cache."""
    global _primary_provider_cache
    try:
        from app.repos.settings_repo import get_global_setting

        db_value = await get_global_setting("primary_provider", "")
        if db_value:
            _primary_provider_cache = db_value
            return db_value
    except Exception:
        pass  # DB unavailable — fall through to env
    try:
        env_value: str = config_manager.get_setting("PRIMARY_PROVIDER", "opencode")
    except Exception:
        env_value = os.getenv("PRIMARY_PROVIDER", "opencode").strip().lower() or "opencode"
    _primary_provider_cache = env_value
    return env_value
