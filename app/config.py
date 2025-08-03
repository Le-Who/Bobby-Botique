import pytz
from typing import List, Dict
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Defines and validates all application settings using Pydantic.
    Reads from environment variables and/or a .env file.
    """
    # This is the modern Pydantic v2 way to configure settings behavior.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
        # THIS IS THE FIX: Tell Pydantic to split comma-separated strings
        # for fields that are typed as lists (e.g., List[str]).
        env_separator=','
    )

    # --- CORE (Required from .env) ---
    TELEGRAM_BOT_TOKEN: str
    GEMINI_API_KEYS: List[str]  # Will now be parsed correctly from "key1,key2,..."
    TAVILY_API_KEYS: List[str]  # Same for this one
    DATABASE_URL: str
    ADMIN_ID: int

    # --- CORE (With defaults) ---
    # Use Field(alias=...) for Render's uppercase PORT variable
    PORT: int = Field(default=10000, alias="PORT")

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


# --- TIMEZONES (not part of BaseSettings as they are not from env) ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
# The entire application will import this `settings` object
try:
    settings = Settings()
except Exception as e:
    # Provides a much clearer error message on startup if .env is misconfigured.
    print(f"FATAL: Could not load settings. Please check your .env file. Error: {e}")
    exit(1)
