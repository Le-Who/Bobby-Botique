import pytz
from typing import List, Dict, Any
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Defines and validates all application settings using Pydantic.
    Reads from environment variables and/or a .env file.
    This version uses a model_validator for robust parsing of env vars from hosting providers.
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
    SAFETY_SETTINGS: List[Dict[str, str]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # THIS IS THE FIX: A robust model_validator that pre-processes raw env vars.
    @model_validator(mode='before')
    @classmethod
    def preprocess_comma_separated_lists(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        This validator runs before any other validation. It finds fields that are
        expected to be lists but are provided as comma-separated strings, and
        correctly converts them.
        """
        list_fields = ['GEMINI_API_KEYS', 'TAVILY_API_KEYS']
        for field in list_fields:
            value = values.get(field)
            if isinstance(value, str):
                # Clean and split the string into a list
                cleaned_v = value.strip().strip('"').strip("'")
                values[field] = [key.strip() for key in cleaned_v.split(',') if key.strip()]
        return values


# --- TIMEZONES (not part of BaseSettings as they are not from env) ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
try:
    settings = Settings()
except Exception as e:
    print(f"FATAL: Could not load settings. Please check your .env file. Error: {e}")
    exit(1)
