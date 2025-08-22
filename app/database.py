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
import time

from app.config import settings, PACIFIC_TZ

db_pool: Optional[Pool] = None

@dataclass
class ChatState:
    history: List[Dict[str, Any]]
    model: str
    token_count: int
    search_enabled: bool
    system_prompt: Optional[str]
    is_deep_dive: bool = False

# CRITICAL: Function removed - prepared statements are disabled for PgBouncer compatibility
# All queries must use $1, $2, $3 format directly

async def _create_db_pool():
    """Создает пул соединений с базой данных с настройками для Supabase.com"""
    try:
        pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL, 
            min_size=2, 
            max_size=10,  # Optimized for Supabase.com free tier (200 concurrent connections)
            command_timeout=60,  # 60 seconds timeout for Supabase
            statement_cache_size=0,  # CRITICAL: Disable prepared statements for PgBouncer compatibility
            server_settings={
                'application_name': 'gemaibotv2',
                'tcp_keepalives_idle': '60',
                'tcp_keepalives_interval': '10',
                'tcp_keepalives_count': '6'
            }
        )
        
        # Start connection pool monitoring
        asyncio.create_task(_monitor_connection_pool(pool))
        
        return pool
    except Exception as e:
        if "rate limit" in str(e).lower() or "quota" in str(e).lower():
            logging.critical("Supabase.com rate limit exceeded. Please upgrade your plan or wait for quota reset.")
            raise Exception(f"Database rate limit exceeded: {e}")
        elif "connection" in str(e).lower() or "timeout" in str(e).lower():
            logging.warning("Database connection issue: %s. This might be temporary.", e)
            raise Exception(f"Database connection failed: {e}")
        else:
            logging.error("Unexpected database error: %s", e)
            raise Exception(f"Database initialization failed: {e}")


async def _monitor_connection_pool(pool):
    """Мониторинг состояния пула соединений с базой данных"""
    while True:
        try:
            if pool and not pool._closed:
                # Получаем статистику пула
                pool_stats = {
                    'min_size': pool._minsize,
                    'max_size': pool._maxsize,
                    'size': pool._size,
                    'free_size': pool._free_size,
                    'in_use': pool._size - pool._free_size,
                    'utilization': (pool._size - pool._free_size) / pool._maxsize * 100 if pool._maxsize > 0 else 0
                }
                
                # Логируем статистику каждые 30 секунд
                logging.debug("Database pool stats: %s", pool_stats)
                
                # Предупреждение при высокой утилизации
                if pool_stats['utilization'] > 80:
                    logging.warning("Database pool high utilization: %.1f%%", pool_stats['utilization'])
                
                # Предупреждение при нехватке свободных соединений
                if pool_stats['free_size'] == 0:
                    logging.warning("Database pool exhausted - no free connections available")
                
            await asyncio.sleep(30)  # Проверяем каждые 30 секунд
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error("Connection pool monitoring error: %s", e)
            await asyncio.sleep(60)  # При ошибке ждем дольше

async def reconnect_database():
    """Переподключается к базе данных при потере соединения"""
    global db_pool
    try:
        logging.info("Attempting to reconnect to database...")
        if db_pool:
            await db_pool.close()
            logging.info("Closed existing database pool")
        
        db_pool = await _create_db_pool()
        logging.info("Database reconnected successfully")
        return True
    except Exception as e:
        logging.critical(f"Failed to reconnect to database: {e}")
        return False

async def db_query(query: str, params: tuple = (), retries: int = 3):
    if not db_pool:
        logging.critical("Database pool is not initialized - this should not happen!")
        raise Exception("Database pool is not initialized")
    
    # Проверяем состояние пула перед выполнением запроса
    if db_pool._closed:
        logging.warning("Database pool is closed, attempting to reconnect...")
        if not await reconnect_database():
            raise Exception("Failed to reconnect to database")
    
    # CRITICAL: Since statement_cache_size=0, we use direct queries without prepared statements
    last_exception = None

    for attempt in range(retries):
        try:
            async with db_pool.acquire() as conn:
                # Apply Supabase-specific session settings for each connection
                try:
                    await conn.execute("SET statement_timeout = '60s'")
                    await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
                except Exception as opt_e:
                    logging.debug(f"Failed to set session optimizations: {opt_e}")
                
                if query.strip().upper().startswith("SELECT"):
                    return await conn.fetch(query, *params)
                else:
                    await conn.execute(query, *params)
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
                                return await conn.fetch(query, *params)
                            else:
                                await conn.execute(query, *params)
                                return None
                    except Exception as final_e:
                        logging.critical(f"Query failed even after reconnection: {final_e}")
                        raise final_e
                else:
                    logging.critical("Failed to reconnect to database, raising original error")
                    raise last_exception
            await asyncio.sleep(1 + attempt)
        except asyncpg.exceptions.QueryCanceledError as e:
            # Handle Supabase query timeout specifically
            if "statement timeout" in str(e).lower():
                logging.warning(f"Query timeout on Supabase (attempt {attempt + 1}/{retries}): {e}")
                if attempt == retries - 1:
                    raise Exception(f"Query timeout after {retries} attempts: {e}")
            else:
                raise e
        except Exception as e:
            logging.error(f"An unexpected database error occurred during query: {query[:100]}... - {e}", exc_info=False)
            raise e

    logging.error("All database retries failed.")
    raise last_exception

async def init_db():
    global db_pool
    if not settings.DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    db_pool = await _create_db_pool()
    
    # Apply Supabase-specific optimizations
    await optimize_database_connections()
    
    await db_query("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0, is_deep_dive BOOLEAN DEFAULT FALSE)""")
    await db_query("""CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)""")
    await db_query("""CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
    await db_query("""CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))""")
    await db_query("""CREATE TABLE IF NOT EXISTS tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
    await db_query("""CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))""")
    await db_query("DROP TABLE IF EXISTS user_documents;")
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
        # --- Document Table Migration ---
        doc_columns = await db_query("SELECT column_name FROM information_schema.columns WHERE table_name='user_documents'")
        doc_column_names = {c['column_name'] for c in doc_columns}

        # 2. Check for 'filename' (and rename from 'file_name' if necessary)
        if 'filename' not in doc_column_names and 'file_name' in doc_column_names:
            await db_query("ALTER TABLE user_documents RENAME COLUMN file_name TO filename;")
            logging.info("Migration: Renamed 'file_name' to 'filename' in 'user_documents'.")
        elif 'filename' not in doc_column_names:
             await db_query("ALTER TABLE user_documents ADD COLUMN filename TEXT;")
             logging.info("Migration: Added 'filename' column to 'user_documents'.")

        # 3. Add other missing columns
        required_columns = {
            "content": "TEXT",
            "pages": "INTEGER",
            "file_size": "BIGINT",
            "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"
        }
        for col, col_type in required_columns.items():
            if col not in doc_column_names:
                await db_query(f"ALTER TABLE user_documents ADD COLUMN {col} {col_type};")
                logging.info(f"Migration: Added '{col}' column to 'user_documents'.")

        # --- Tavily Key Usage Migration ---
        tavily_columns = await db_query("SELECT column_name FROM information_schema.columns WHERE table_name='tavily_key_usage'")
        if 'request_count' in {c['column_name'] for c in tavily_columns}:
            logging.info("Old column 'request_count' found in 'tavily_key_usage'. Attempting schema migration...")
            await db_query("ALTER TABLE tavily_key_usage RENAME COLUMN request_count TO credit_usage;")
            logging.info("Schema migration for 'tavily_key_usage' successful.")

        # --- Users Table Migration (is_deep_dive) ---
        users_columns = await db_query("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
        if 'is_deep_dive' not in {c['column_name'] for c in users_columns}:
            logging.info("Column 'is_deep_dive' not found in 'users' table. Attempting schema migration...")
            await db_query("ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;")
            logging.info("Schema migration for 'is_deep_dive' successful.")

    except asyncpg.PostgresError as e:
        logging.warning(f"A schema migration may have been skipped or failed: {e}")
    
    await db_query("INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING", (settings.ADMIN_ID,))
    for key in settings.GEMINI_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))
    for key in settings.TAVILY_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))

async def get_user_chat(user_id: int) -> ChatState:
    chat_result = await db_query("SELECT * FROM chats WHERE user_id = $1", (user_id,))
    user_result = await db_query("SELECT is_deep_dive FROM users WHERE user_id = $1", (user_id,))

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
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (user_id)
    DO UPDATE SET
        history = EXCLUDED.history, model = EXCLUDED.model, token_count = EXCLUDED.token_count,
        search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt;
    """
    await db_query(chat_query, (user_id, history_json, chat_state.model, chat_state.token_count, int(chat_state.search_enabled), chat_state.system_prompt))

    user_query = "UPDATE users SET is_deep_dive = $1 WHERE user_id = $2"
    await db_query(user_query, (chat_state.is_deep_dive, user_id))

async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    today_pacific: date = datetime.now(PACIFIC_TZ).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys")
        return keys[0] if keys else None
    all_keys = await db_query("SELECT * FROM api_keys")
    for key_row in all_keys:
        usage = await db_query("SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3", (key_row['key_hash'], model_name, today_pacific))
        request_count = usage[0]['request_count'] if usage else 0
        if request_count < daily_limit * settings.LIMIT_THRESHOLD_PERCENT:
            return key_row
    return None

async def increment_gemini_key_usage(key_hash: str, model_name: str):
    today_pacific: date = datetime.now(PACIFIC_TZ).date()
    query = """
                INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, 1)
    ON CONFLICT (key_hash, model_name, usage_date)
    DO UPDATE SET request_count = key_usage.request_count + 1;
    """
    await db_query(query, (key_hash, model_name, today_pacific))

async def get_available_tavily_key():
    current_month = datetime.now(pytz.utc).strftime('%Y-%m')
    all_keys = await db_query("SELECT * FROM tavily_api_keys")
    for key_row in all_keys:
        usage = await db_query("SELECT credit_usage FROM tavily_key_usage WHERE key_hash = $1 AND usage_month = $2", (key_row['key_hash'], current_month))
        credit_usage = usage[0]['credit_usage'] if usage else 0
        if credit_usage < settings.TAVILY_MONTHLY_CREDIT_LIMIT * settings.TAVILY_LIMIT_THRESHOLD_PERCENT:
            return key_row
    return None

async def increment_tavily_key_usage(key_hash: str, cost: int):
    current_month = datetime.now(pytz.utc).strftime('%Y-%m')
    query = """
    INSERT INTO tavily_key_usage (key_hash, usage_month, credit_usage) VALUES ($1, $2, $3)
    ON CONFLICT (key_hash, usage_month)
    DO UPDATE SET credit_usage = tavily_key_usage.credit_usage + $4;
    """
    await db_query(query, (key_hash, current_month, cost, cost))

async def check_database_health():
    """Проверяет здоровье соединения с базой данных с оптимизациями для Supabase"""
    if not db_pool:
        logging.warning("Database pool not initialized")
        return False
    
    if db_pool._closed:
        logging.warning("Database pool is closed")
        return False
    
    try:
        async with db_pool.acquire() as conn:
            # Use a simple health check query optimized for Supabase
            await conn.execute("SELECT 1")
            return True
    except Exception as e:
        logging.warning(f"Database health check failed: {e}")
        return False

async def ensure_database_connection():
    """Обеспечивает активное соединение с базой данных с оптимизациями для Supabase"""
    if not await check_database_health():
        logging.info("Database connection unhealthy, attempting reconnection...")
        try:
            return await reconnect_database()
        except Exception as e:
            logging.warning(f"Database reconnection failed: {e}")
            return False
    return True

async def optimize_database_connections():
    """Оптимизирует соединения с базой данных для Supabase free tier"""
    if not db_pool:
        return False
    
    try:
        async with db_pool.acquire() as conn:
            # Set session-level optimizations for Supabase
            await conn.execute("SET statement_timeout = '60s'")
            await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
            await conn.execute("SET lock_timeout = '30s'")
        logging.info("Database session optimizations applied for Supabase")
        return True
    except Exception as e:
        logging.warning(f"Failed to apply database optimizations: {e}")
        return False

async def get_supabase_metrics() -> Dict[str, Any]:
    """Возвращает метрики производительности базы данных, оптимизированные для Supabase"""
    if not db_pool:
        return {"status": "disconnected", "pool_size": 0, "active_connections": 0}
    
    try:
        # Get pool statistics
        pool_stats = {
            "status": "connected" if not db_pool._closed else "closed",
            "pool_size": db_pool.get_size(),
            "free_size": db_pool.get_free_size(),
            "active_connections": db_pool.get_size() - db_pool.get_free_size()
        }
        
        # Test connection performance
        start_time = time.time()
        async with db_pool.acquire() as conn:
            await conn.execute("SELECT 1")
            response_time = time.time() - start_time
            
            pool_stats.update({
                "response_time_ms": round(response_time * 1000, 2),
                "connection_health": "healthy" if response_time < 0.1 else "slow"
            })
        
        return pool_stats
        
    except Exception as e:
        logging.warning(f"Failed to get Supabase metrics: {e}")
        return {
            "status": "error",
            "error": str(e),
            "pool_size": 0,
            "active_connections": 0
        }

async def check_supabase_limits():
    """Проверяет текущие лимиты Supabase free tier"""
    try:
        async with db_pool.acquire() as conn:
            # Check current database size (free tier has 500MB limit)
            size_result = await conn.fetch("""
                SELECT pg_size_pretty(pg_database_size(current_database())) as db_size,
                       pg_database_size(current_database()) as db_size_bytes
            """)
            
            db_size = size_result[0]['db_size']
            db_size_bytes = size_result[0]['db_size_bytes']
            
            # Check active connections (free tier has 200 concurrent connection limit)
            conn_result = await conn.fetch("""
                SELECT count(*) as active_connections 
                FROM pg_stat_activity 
                WHERE state = 'active' AND datname = current_database()
            """)
            
            active_connections = conn_result[0]['active_connections']
            
            return {
                "database_size": db_size,
                "database_size_bytes": db_size_bytes,
                "active_connections": active_connections,
                "free_tier_limits": {
                    "max_database_size_mb": 500,
                    "max_concurrent_connections": 200,
                    "max_messages_per_month": 2000000,
                    "max_message_size_kb": 250
                },
                "usage_percentage": {
                    "database": round((db_size_bytes / (500 * 1024 * 1024)) * 100, 1),
                    "connections": round((active_connections / 200) * 100, 1)
                }
            }
            
    except Exception as e:
        logging.warning(f"Failed to check Supabase limits: {e}")
        return {"error": str(e)}

def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID

async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    result = await db_query("SELECT is_authorized FROM users WHERE user_id = $1", (user_id,))
    return result and result[0]['is_authorized'] == 1
