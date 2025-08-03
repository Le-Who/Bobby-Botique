import logging
import json
import hashlib
import asyncio
from datetime import datetime
import pytz
import asyncpg
from asyncpg.pool import Pool
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from . import config

db_pool: Optional[Pool] = None

@dataclass
class ChatState:
    history: List[Dict[str, Any]]
    model: str
    token_count: int
    search_enabled: bool
    system_prompt: Optional[str]

def _prepare_query(query: str) -> str:
    """Replaces '?' and '%s' with numbered placeholders like $1, $2."""
    query_prepared = query.replace('?', '$').replace('%s', '$')
    count = query_prepared.count('$')
    for i in range(1, count + 1):
        query_prepared = query_prepared.replace('$', f'${i}', 1)
    return query_prepared

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
            logging.error(f"An unexpected database error occurred: {e}", exc_info=True)
            raise e

    logging.error("All database retries failed.")
    raise last_exception

async def init_db():
    global db_pool
    if not config.DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    db_pool = await asyncpg.create_pool(dsn=config.DATABASE_URL, min_size=1, max_size=10)
    
    await db_query("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0)""")
    await db_query("""CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)""")
    await db_query("""CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
    await db_query("""CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))""")
    await db_query("""CREATE TABLE IF NOT EXISTS tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
    await db_query("""CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))""")
    
    try:
        await db_query("ALTER TABLE tavily_key_usage RENAME COLUMN request_count TO credit_usage;")
        logging.info("Schema migration successful.")
    except asyncpg.exceptions.DuplicateColumnError:
        logging.info("Schema migration not needed or already applied.")
    
    await db_query("INSERT INTO users (user_id, is_authorized) VALUES (?, 1) ON CONFLICT (user_id) DO NOTHING", (config.ADMIN_ID,))
    for key in config.GEMINI_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO api_keys (key_hash, api_key) VALUES (?, ?) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))
    for key in config.TAVILY_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO tavily_api_keys (key_hash, api_key) VALUES (?, ?) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))

async def get_user_chat(user_id: int) -> ChatState:
    result = await db_query("SELECT * FROM chats WHERE user_id = ?", (user_id,))
    if result:
        row = result[0]
        return ChatState(
            history=json.loads(row['history']) if row['history'] else [],
            model=row['model'] or config.DEFAULT_MODEL,
            token_count=row['token_count'] or 0,
            search_enabled=bool(row['search_enabled']),
            system_prompt=row['system_prompt'] or None
        )
    return ChatState(history=[], model=config.DEFAULT_MODEL, token_count=0, search_enabled=False, system_prompt=None)

async def update_user_chat(user_id: int, chat_state: ChatState):
    history_json = json.dumps(chat_state.history)
    query = """
    INSERT INTO chats (user_id, history, model, token_count, search_enabled, system_prompt) 
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (user_id) 
    DO UPDATE SET 
        history = EXCLUDED.history, model = EXCLUDED.model, token_count = EXCLUDED.token_count, 
        search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt;
    """
    await db_query(query, (user_id, history_json, chat_state.model, chat_state.token_count, int(chat_state.search_enabled), chat_state.system_prompt))

async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    today_pacific = datetime.now(config.PACIFIC_TZ).strftime('%Y-%m-%d')
    daily_limit = config.DAILY_LIMITS.get(model_name)
    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys")
        return keys[0] if keys else None
    all_keys = await db_query("SELECT * FROM api_keys")
    for key_row in all_keys:
        usage = await db_query("SELECT request_count FROM key_usage WHERE key_hash = ? AND model_name = ? AND usage_date = ?", (key_row['key_hash'], model_name, today_pacific))
        request_count = usage[0]['request_count'] if usage else 0
        if request_count < daily_limit * config.LIMIT_THRESHOLD_PERCENT:
            return key_row
    return None

async def increment_gemini_key_usage(key_hash: str, model_name: str):
    today_pacific = datetime.now(config.PACIFIC_TZ).strftime('%Y-%m-%d')
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
        if credit_usage < config.TAVILY_MONTHLY_CREDIT_LIMIT * config.TAVILY_LIMIT_THRESHOLD_PERCENT:
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
    return user_id == config.ADMIN_ID

async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    result = await db_query("SELECT is_authorized FROM users WHERE user_id = ?", (user_id,))
    return result and result[0]['is_authorized'] == 1
