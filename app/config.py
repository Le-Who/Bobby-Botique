import pytz
from typing import List, Dict, Any
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Defines and validates all application settings using Pydantic.
    Reads from environment variables and/or a .env file.
    This version is robust to handle quoted environment variables from hosting providers like Render.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
    )

    # --- CORE (Required from .env) ---
    TELEGRAM_BOT_TOKEN: str
    GEMINI_API_KEYS: List[str]
    TAVILY_API_KEYS: List[str]
    DATABASE_URL: str
    ADMIN_ID: int

    # --- CORE (With defaults) ---
    PORT: int = Field(default=10000, alias="PORT")

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096

    # --- MODELS (Corrected Names) ---
    AVAILABLE_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"]
    DEFAULT_MODEL: str = "gemini-2.5-flash"
    QNA_MODEL: str = "gemini-2.5-flash-lite"
    RESEARCH_MODEL: str = "gemini-2.5-pro"
    URL_SELECTION_MODEL: str = "gemini-2.5-flash"

    # --- LIMITS (Corrected Names) ---
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
    SAFETY_SETTINGS: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    @field_validator('GEMINI_API_KEYS', 'TAVILY_API_KEYS', mode='before')
    @classmethod
    def split_and_clean_str(cls, v: Any) -> List[str]:
        """
        Takes a raw value from an env var, cleans it, and splits it into a list.
        Handles cases where the string is quoted (e.g., "key1,key2").
        """
        if isinstance(v, str):
            cleaned_v = v.strip().strip('"').strip("'")
            return [key.strip() for key in cleaned_v.split(',') if key.strip()]
        if isinstance(v, list):
            return v
        return []


# --- TIMEZONES (not part of BaseSettings as they are not from env) ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
try:
    settings = Settings()
except Exception as e:
    print(f"FATAL: Could not load settings. Please check your .env file. Error: {e}")
    exit(1)
