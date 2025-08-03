import os
import pytz

# --- CORE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEYS = [key.strip() for key in os.getenv("GEMINI_API_KEYS", "").split(',') if key.strip()]
TAVILY_API_KEYS = [key.strip() for key in os.getenv("TAVILY_API_KEYS", "").split(',') if key.strip()]
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.environ.get('PORT', 10000))
ADMIN_ID = 5726630815

# --- CHAT ---
CHAT_TOKEN_LIMIT = 384000
TELEGRAM_MESSAGE_LIMIT = 4096

# --- MODELS ---
AVAILABLE_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"]
DEFAULT_MODEL = "gemini-2.5-flash"
QNA_MODEL = "gemini-2.5-flash-lite"
RESEARCH_MODEL = "gemini-2.5-pro"
URL_SELECTION_MODEL = "gemini-2.5-flash"

# --- LIMITS ---
TAVILY_MONTHLY_CREDIT_LIMIT = 1000
TAVILY_LIMIT_THRESHOLD_PERCENT = 0.97
TAVILY_QNA_SEARCH_COST = 2
TAVILY_ADVANCED_SEARCH_COST = 2

DAILY_LIMITS = {
    "gemini-2.5-flash": 250,
    "gemini-2.5-pro": 100,
    "gemini-2.5-flash-lite": 1000,
}
LIMIT_THRESHOLD_PERCENT = 0.95

# --- TIMEZONES ---
PACIFIC_TZ = pytz.timezone('US/Pacific')
KYIV_TZ = pytz.timezone('Europe/Kyiv')

# --- SAFETY ---
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]
