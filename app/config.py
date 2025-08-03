import os
import pytz
from typing import List, Dict, Any
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _get_keys_from_env(env_var_name: str) -> List[str]:
    """
    A robust, manual function to get comma-separated keys from an environment variable.
    This function is called directly to bypass Pydantic's problematic parsing.
    """
    value = os.getenv(env_var_name)
    if not value:
        # If the variable is not set at all, raise an error.
        raise ValueError(f"Required environment variable '{env_var_name}' is not set.")
    
    # Clean the string from quotes and whitespace, then split.
    cleaned_v = value.strip().strip('"').strip("'")
    return [key.strip() for key in cleaned_v.split(',') if key.strip()]

class Settings(BaseSettings):
    """
    Defines and validates all application settings using Pydantic.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
    )

    # --- SIMPLE FIELDS (Loaded automatically by Pydantic) ---
    TELEGRAM_BOT_TOKEN: str
    DATABASE_URL: str
    ADMIN_ID: int
    PORT: int = Field(default=10000, alias="PORT")

    # --- COMPLEX FIELDS (Loaded manually via default_factory) ---
    # `exclude=True` tells Pydantic to NOT look for these in the environment.
    # `default_factory` calls our manual function to get the value.
    GEMINI_API_KEYS: List[str] = Field(default_factory=lambda: _get_keys_from_env("GEMINI_API_KEYS"), exclude=True)
    TAVILY_API_KEYS: List[str] = Field(default_factory=lambda: _get_keys_from_env("TAVILY_API_KEYS"), exclude=True)

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

    # After our manual factory provides the value, we can still validate it.
    @field_validator('GEMINI_API_KEYS', 'TAVILY_API_KEYS')
    @classmethod
    def check_lists_not_empty(cls, v: List[str], info) -> List[str]:
        if not v:
            raise ValueError(f"List for field '{info.field_name}' cannot be empty.")
        return v

# --- TIMEZONES (not part of BaseSettings as they are not from env) ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SINGLETON INSTANCE ---
try:
    settings = Settings()
except Exception as e:
    print(f"FATAL: Could not load settings. Please check your .env file or environment variables. Error: {e}")
    exit(1)
