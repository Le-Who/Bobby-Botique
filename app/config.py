import os
import pytz
from typing import List, Dict
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
   - НИКОГДА не используй LaTeX: `$1 \times 1 = 1$` или `$$\sqrt{2}$$`
   - ВСЕГДА используй обычный текст: `1 × 1 = 1` или `√2` или `корень из 2`
   - Для дробей: используй `/` (например, `1/2` вместо `$\frac{1}{2}$`)
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
            "PORT": os.getenv("PORT", 10000), # Provide a default for PORT
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
