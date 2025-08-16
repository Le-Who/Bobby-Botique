import logging
import json
import hashlib
import asyncio
import re
from datetime import datetime, date
import pytz
import asyncpg
from asyncpg.pool import Pool
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from .config import settings, PACIFIC_TZ

db_pool: Optional[Pool] = None

@dataclass
class ChatState:
    history: List[Dict[str, Any]]
    model: str
    token_count: int
    search_enabled: bool
    system_prompt: Optional[str]
    is_deep_dive: bool = False

def _prepare_query(query: str) -> str:
    placeholders = re.findall(r'(\?|%s)', query)
    for i, _ in enumerate(placeholders, 1):
        query = re.sub(r'(\?|%s)', f'${i}', query, 1)
    return query

async def db_query(query: str, params: tuple = (), retries: int = 3):
    if not db_pool:
        raise Exception("Database pool is not initialized")
    
    query_prepared = _prepare_query(query)
    last_exception = None

    for attempt in range(retries):
        try:
            async with db_pool.acquire() as conn:
                if query.strip().upper().startswith("SELECT"):
                    return await conn.fetch(query_prepared, *params)
                else:
                    await conn.execute(query_prepared, *params)
                    return None
        except (asyncpg.exceptions.ConnectionDoesNotExistError, OSError) as e:
            logging.warning(f"DB connection error (attempt {attempt + 1}/{retries}): {e}. Retrying...")
            last_exception = e
            await asyncio.sleep(1 + attempt)
        except Exception as e:
            logging.error(f"An unexpected database error occurred during query: {query_prepared[:100]}... - {e}", exc_info=False)
            raise e

    logging.error("All database retries failed.")
    raise last_exception

async def init_db():
    global db_pool
    if not settings.DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    db_pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL, min_size=1, max_size=10)
    
    await db_query("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0, is_deep_dive BOOLEAN DEFAULT FALSE)""")
    await db_query("""CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)""")
    await db_query("""CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
    await db_query("""CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))""")
    await db_query("""CREATE TABLE IF NOT EXISTS tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
    await db_query("""CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))""")
    await db_query("""
        CREATE TABLE IF NOT EXISTS user_documents (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            filename TEXT,
            content TEXT,
            pages INTEGER,
            file_size BIGINT,
            file_hash TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, file_hash)
        )
    """)

    try:
        # Migration for tavily_key_usage table
        check_column_query = "SELECT 1 FROM information_schema.columns WHERE table_name='tavily_key_usage' AND column_name='request_count';"
        column_exists = await db_query(check_column_query)
        if column_exists:
            logging.info("Old column 'request_count' found. Attempting schema migration...")
            await db_query("ALTER TABLE tavily_key_usage RENAME COLUMN request_count TO credit_usage;")
            logging.info("Schema migration successful.")
        
        # Migration for users table to add is_deep_dive
        check_deep_dive_column_query = "SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='is_deep_dive';"
        deep_dive_exists = await db_query(check_deep_dive_column_query)
        if not deep_dive_exists:
            logging.info("Column 'is_deep_dive' not found in users table. Attempting schema migration...")
            await db_query("ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;")
            logging.info("Schema migration for 'is_deep_dive' successful.")
            
    except asyncpg.PostgresError as e:
        logging.info(f"Schema migration skipped or already applied (Error: {e})")
    
    await db_query("INSERT INTO users (user_id, is_authorized) VALUES (?, 1) ON CONFLICT (user_id) DO NOTHING", (settings.ADMIN_ID,))
    for key in settings.GEMINI_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO api_keys (key_hash, api_key) VALUES (?, ?) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))
    for key in settings.TAVILY_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO tavily_api_keys (key_hash, api_key) VALUES (?, ?) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))

async def get_user_chat(user_id: int) -> ChatState:
    chat_result = await db_query("SELECT * FROM chats WHERE user_id = ?", (user_id,))
    user_result = await db_query("SELECT is_deep_dive FROM users WHERE user_id = ?", (user_id,))

    chat_state = ChatState(history=[], model=settings.DEFAULT_MODEL, token_count=0, search_enabled=False, system_prompt=None, is_deep_dive=False)

    if chat_result:
        row = chat_result[0]
        chat_state.history = json.loads(row['history']) if row['history'] else []
        chat_state.model = row['model'] or settings.DEFAULT_MODEL
        chat_state.token_count = row['token_count'] or 0
        chat_state.search_enabled = bool(row['search_enabled'])
        chat_state.system_prompt = row['system_prompt'] or None

    if user_result:
        chat_state.is_deep_dive = user_result[0]['is_deep_dive'] or False
        
    return chat_state

async def update_user_chat(user_id: int, chat_state: ChatState):
    history_json = json.dumps(chat_state.history)
    chat_query = """
    INSERT INTO chats (user_id, history, model, token_count, search_enabled, system_prompt)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (user_id)
    DO UPDATE SET
        history = EXCLUDED.history, model = EXCLUDED.model, token_count = EXCLUDED.token_count,
        search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt;
    """
    await db_query(chat_query, (user_id, history_json, chat_state.model, chat_state.token_count, int(chat_state.search_enabled), chat_state.system_prompt))

    user_query = "UPDATE users SET is_deep_dive = ? WHERE user_id = ?"
    await db_query(user_query, (chat_state.is_deep_dive, user_id))

async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    today_pacific: date = datetime.now(PACIFIC_TZ).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys")
        return keys[0] if keys else None
    all_keys = await db_query("SELECT * FROM api_keys")
    for key_row in all_keys:
        usage = await db_query("SELECT request_count FROM key_usage WHERE key_hash = ? AND model_name = ? AND usage_date = ?", (key_row['key_hash'], model_name, today_pacific))
        request_count = usage[0]['request_count'] if usage else 0
        if request_count < daily_limit * settings.LIMIT_THRESHOLD_PERCENT:
            return key_row
    return None

async def increment_gemini_key_usage(key_hash: str, model_name: str):
    today_pacific: date = datetime.now(PACIFIC_TZ).date()
    query = """
    INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES (?, ?, ?, 1)
    ON CONFLICT (key_hash, model_name, usage_date)
    DO UPDATE SET request_count = key_usage.request_count + 1;
    """
    await db_query(query, (key_hash, model_name, today_pacific))

async def get_available_tavily_key():
    current_month = datetime.now(pytz.utc).strftime('%Y-%m')
    all_keys = await db_query("SELECT * FROM tavily_api_keys")
    for key_row in all_keys:
        usage = await db_query("SELECT credit_usage FROM tavily_key_usage WHERE key_hash = ? AND usage_month = ?", (key_row['key_hash'], current_month))
        credit_usage = usage[0]['credit_usage'] if usage else 0
        if credit_usage < settings.TAVILY_MONTHLY_CREDIT_LIMIT * settings.TAVILY_LIMIT_THRESHOLD_PERCENT:
            return key_row
    return None

async def increment_tavily_key_usage(key_hash: str, cost: int):
    current_month = datetime.now(pytz.utc).strftime('%Y-%m')
    query = """
    INSERT INTO tavily_key_usage (key_hash, usage_month, credit_usage) VALUES (?, ?, ?)
    ON CONFLICT (key_hash, usage_month)
    DO UPDATE SET credit_usage = tavily_key_usage.credit_usage + ?;
    """
    await db_query(query, (key_hash, current_month, cost, cost))

def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID

async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    result = await db_query("SELECT is_authorized FROM users WHERE user_id = ?", (user_id,))
    return result and result[0]['is_authorized'] == 1
