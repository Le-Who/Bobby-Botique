# /app/config.py

import pytz
from typing import List, Dict, Any
from pydantic import field_validator, Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # --- CORE (Required from .env) ---
    TELEGRAM_BOT_TOKEN: str
    GEMINI_API_KEYS: List[str]
    TAVILY_API_KEYS: List[str]
    DATABASE_URL: str
    ADMIN_ID: int

    # --- CORE (With defaults) ---
    PORT: int = Field(default=10000, env="PORT")

    # --- CHAT ---
    CHAT_TOKEN_LIMIT: int = 384000
    TELEGRAM_MESSAGE_LIMIT: int = 4096

    # --- MODELS ---
    AVAILABLE_MODELS: List[str] = ["gemini-1.5-flash", "gemini-1.5-pro"]
    DEFAULT_MODEL: str = "gemini-1.5-flash"
    QNA_MODEL: str = "gemini-1.5-flash"
    RESEARCH_MODEL: str = "gemini-1.5-pro"
    URL_SELECTION_MODEL: str = "gemini-1.5-flash"

    # --- LIMITS ---
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97
    TAVILY_QNA_SEARCH_COST: int = 2
    TAVILY_ADVANCED_SEARCH_COST: int = 2
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    DAILY_LIMITS: Dict[str, int] = {
        "gemini-1.5-flash": 1000,
        "gemini-1.5-pro": 100,
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
    def split_str(cls, v: Any) -> List[str]:
        """Automatically splits comma-separated strings from .env into a list."""
        if isinstance(v, str):
            return [key.strip() for key in v.split(',') if key.strip()]
        return v

    class Config:
        # Pydantic will read from a .env file if it exists
        env_file = ".env"
        env_file_encoding = 'utf-8'

# --- TIMEZONES (not part of BaseSettings as they are not from env) ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
# The entire application will import this `settings` object
settings = Settings()
