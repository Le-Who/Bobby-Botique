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
_last_error: Optional[str] = None  # Для хранения последней ошибки БД

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

async def reconnect_database():
    """Переподключается к базе данных при потере соединения"""
    global db_pool
    try:
        logging.info("Attempting to reconnect to database...")
        if db_pool:
            await db_pool.close()
            logging.info("Closed existing database pool")
        
        # Neon.tech free tier supports max 5 connections
        db_pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL, 
            min_size=1, 
            max_size=3,  # Conservative limit for Neon.tech free tier
            command_timeout=30,  # 30 seconds timeout for serverless
            server_settings={'application_name': 'gemaibotv2'},
            ssl='require'  # Принудительно используем SSL
        )
        logging.info("Database reconnected successfully")
        return True
    except Exception as e:
        logging.critical(f"Failed to reconnect to database: {e}")
        return False

async def db_query(query: str, params: tuple = (), retries: int = 3):
    global db_pool
    
    # Если пул не инициализирован, пытаемся инициализировать
    if not db_pool:
        logging.warning("Database pool not initialized, attempting to initialize...")
        try:
            await init_db()
            logging.info("Database pool initialized successfully during query")
        except Exception as init_error:
            if "blocked network" in str(init_error).lower() or "neon.tech" in str(init_error).lower():
                logging.warning("Database blocked by Neon.tech - bot will work in limited mode")
                raise Exception("Database blocked by Neon.tech - working in limited mode")
            else:
                logging.critical(f"Failed to initialize database during query: {init_error}")
                raise Exception(f"Database initialization failed: {init_error}")
    
    # Проверяем, не закрыт ли пул
    if hasattr(db_pool, 'is_closed') and db_pool.is_closed():
        logging.warning("Database pool is closed, attempting to reconnect...")
        try:
            await reconnect_database()
        except Exception as reconnect_error:
            logging.error(f"Failed to reconnect to database: {reconnect_error}")
            raise Exception("Database pool is closed and reconnection failed")
    
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
            if attempt == retries - 1:
                logging.critical(f"All database retries failed. Last error: {e}")
                # Попытка переподключения перед финальной ошибкой
                if await reconnect_database():
                    # Если переподключение успешно, попробуем еще раз
                    try:
                        async with db_pool.acquire() as conn:
                            if query.strip().upper().startswith("SELECT"):
                                return await conn.fetch(query_prepared, *params)
                            else:
                                await conn.execute(query_prepared, *params)
                                return None
                    except Exception as final_e:
                        logging.critical(f"Query failed even after reconnection: {final_e}")
                        raise final_e
                else:
                    logging.critical("Failed to reconnect to database, raising original error")
                    raise last_exception
            await asyncio.sleep(1 + attempt)
        except Exception as e:
            logging.error(f"An unexpected database error occurred during query: {query_prepared[:100]}... - {e}", exc_info=False)
            raise e

    logging.error("All database retries failed.")
    raise last_exception

async def init_db():
    global db_pool
    
    # Проверяем, не инициализирован ли уже пул
    if db_pool is not None and not (hasattr(db_pool, 'is_closed') and db_pool.is_closed()):
        logging.info("Database pool already initialized and available")
        return
    
    if not settings.DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    
    try:
        # Neon.tech free tier supports max 5 connections
        db_pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL, 
            min_size=1, 
            max_size=3,  # Conservative limit for Neon.tech free tier
            command_timeout=30,  # 30 seconds timeout for serverless
            server_settings={'application_name': 'gemaibotv2'},
            ssl='require'  # Принудительно используем SSL
        )
        logging.info("Database connection pool created successfully")
    except asyncpg.exceptions.InternalServerError as e:
        if "blocked network" in str(e).lower():
            logging.critical("CRITICAL: Neon.tech is blocking connections from Render's IP address")
            logging.critical("SOLUTION: You need to whitelist Render's IP addresses in Neon.tech console")
            logging.critical("Or use Neon.tech's connection pooling with 'pooler' mode")
            # Сохраняем ошибку для диагностики
            globals()['_last_error'] = str(e)
            raise Exception("Database blocked by Neon.tech - check network configuration")
        else:
            globals()['_last_error'] = str(e)
            raise e
    except Exception as e:
        logging.critical(f"Failed to create database pool: {e}")
        globals()['_last_error'] = str(e)
        raise e
    
    # Создаем таблицы напрямую, без вызова db_query для избежания циклических вызовов
    try:
        async with db_pool.acquire() as conn:
            # Создаем таблицы
            await conn.execute("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0, is_deep_dive BOOLEAN DEFAULT FALSE)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))""")
            
            # Удаляем старую таблицу документов
            await conn.execute("DROP TABLE IF EXISTS user_documents")
            
            # Создаем новую таблицу документов
            await conn.execute("""
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
            
            # Вставляем начальные данные
            await conn.execute("INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING", settings.ADMIN_ID)
            for key in settings.GEMINI_API_KEYS:
                key_hash = hashlib.sha256(key.encode()).hexdigest()
                await conn.execute("INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", key_hash, key)
            for key in settings.TAVILY_API_KEYS:
                key_hash = hashlib.sha256(key.encode()).hexdigest()
                await conn.execute("INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", key_hash, key)
            
        logging.info("Database tables and initial data created successfully")
        
    except Exception as e:
        logging.critical(f"Failed to create database tables: {e}")
        # Закрываем пул при ошибке
        if db_pool:
            await db_pool.close()
            db_pool = None
        raise e

async def get_user_chat(user_id: int) -> ChatState:
    try:
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
        
    except Exception as e:
        if "blocked network" in str(e).lower() or "neon.tech" in str(e).lower():
            logging.warning(f"Database blocked, returning default chat state for user {user_id}")
            # Возвращаем состояние по умолчанию при блокировке БД
            return ChatState(
                history=[], 
                model=settings.DEFAULT_MODEL, 
                token_count=0, 
                search_enabled=False, 
                system_prompt=None, 
                is_deep_dive=False
            )
        else:
            raise e

async def update_user_chat(user_id: int, chat_state: ChatState):
    try:
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
        
    except Exception as e:
        if "blocked network" in str(e).lower() or "neon.tech" in str(e).lower():
            logging.warning(f"Database blocked, skipping chat update for user {user_id}")
            # При блокировке БД просто пропускаем обновление
            return
        else:
            raise e

async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    try:
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
        
    except Exception as e:
        if "blocked network" in str(e).lower() or "neon.tech" in str(e).lower():
            logging.warning(f"Database blocked, returning first Gemini key from settings for model {model_name}")
            # При блокировке БД возвращаем первый ключ из настроек
            if settings.GEMINI_API_KEYS:
                return {"key_hash": "fallback", "api_key": settings.GEMINI_API_KEYS[0]}
            return None
        else:
            raise e

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

def get_last_error() -> Optional[str]:
    """Возвращает последнюю ошибку БД для диагностики"""
    return _last_error

def is_database_available() -> bool:
    """Проверяет, доступна ли база данных"""
    return db_pool is not None and not (hasattr(db_pool, 'is_closed') and db_pool.is_closed())

def get_database_status() -> str:
    """Возвращает статус базы данных для диагностики"""
    if not db_pool:
        return "not_initialized"
    elif hasattr(db_pool, 'is_closed') and db_pool.is_closed():
        return "closed"
    else:
        return "connected"

def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID

async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    result = await db_query("SELECT is_authorized FROM users WHERE user_id = ?", (user_id,))
    return result and result[0]['is_authorized'] == 1
