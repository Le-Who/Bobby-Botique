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

def _prepare_query(query: str) -> str:
    placeholders = re.findall(r'(\?|%s)', query)
    for i, _ in enumerate(placeholders, 1):
        query = re.sub(r'(\?|%s)', f'${i}', query, 1)
    return query

async def db_query(query: str, params: tuple = (), retries: int = 3):
    """Выполняет запрос к БД с унифицированной обработкой параметров.

    Поддерживает передачу параметров в виде кортежа/списка, а также одиночного значения
    (например, строки). Одиночное значение автоматически оборачивается в кортеж,
    чтобы избежать ошибки вида: "the server expects 1 argument, N were passed".
    """
    if not db_pool:
        raise Exception("Database pool is not initialized")

    # Нормализуем параметры: одиночное значение -> (value,), список -> tuple(list), None -> ()
    if params is None:
        normalized_params = ()
    elif isinstance(params, (list, tuple)):
        normalized_params = tuple(params)
    else:
        normalized_params = (params,)

    query_prepared = _prepare_query(query)
    last_exception = None

    for attempt in range(retries):
        try:
            async with db_pool.acquire() as conn:
                if query.strip().upper().startswith("SELECT"):
                    return await conn.fetch(query_prepared, *normalized_params)
                else:
                    await conn.execute(query_prepared, *normalized_params)
                    return None
        except (asyncpg.exceptions.ConnectionDoesNotExistError, OSError) as e:
            logging.warning(f"DB connection error (attempt {attempt + 1}/{retries}): {e}. Retrying...")
            last_exception = e
            await asyncio.sleep(1 + attempt)
        except Exception as e:
            logging.error(
                f"An unexpected database error occurred during query: {query_prepared[:100]}... - {e}",
                exc_info=False,
            )
            raise e

    logging.error("All database retries failed.")
    raise last_exception

async def init_db():
    """Инициализирует базу данных, создавая необходимые таблицы"""
    global db_pool

    try:
        # Инициализируем пул соединений
        if not db_pool:
            db_pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=1,
                max_size=10
            )

        # Создаем таблицу для API ключей Gemini
        await db_query("""
            CREATE TABLE IF NOT EXISTS gemini_api_keys (
                id SERIAL PRIMARY KEY,
                api_key TEXT NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                daily_usage INTEGER DEFAULT 0,
                last_reset_date DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Создаем таблицу для API ключей Tavily
        await db_query("""
            CREATE TABLE IF NOT EXISTS tavily_api_keys (
                id SERIAL PRIMARY KEY,
                api_key TEXT NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                monthly_usage INTEGER DEFAULT 0,
                last_reset_date DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Создаем таблицу для документов пользователей
        await db_query("""
            CREATE TABLE IF NOT EXISTS user_documents (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                filename TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                pages INTEGER,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, file_hash)
            )
        """)

        # Создаем таблицу для настроек бота
        await db_query("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                id SERIAL PRIMARY KEY,
                setting_name TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Создаем остальные таблицы
        await db_query("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0)""")
        await db_query("""CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)""")
        await db_query("""CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)""")
        await db_query("""CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))""")
        await db_query("""CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))""")

        # Создаем таблицу версий схемы
        await db_query("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id SERIAL PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 1,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Проверяем текущую версию схемы
        version_result = await db_query("SELECT version FROM schema_version ORDER BY id DESC LIMIT 1")
        current_version = version_result[0]['version'] if version_result else 0

        # Применяем миграции если необходимо
        if current_version < 1:
            await _apply_migration_1()
            await db_query("INSERT INTO schema_version (version) VALUES (1)")
            logging.info("Schema migration to version 1 completed.")

        logging.info(f"Database schema is at version {current_version + (1 if current_version < 1 else 0)}")

        # Инициализируем базовые данные
        await db_query("INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING", (settings.ADMIN_ID,))
        for key in settings.GEMINI_API_KEYS:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            await db_query("INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))
        for key in settings.TAVILY_API_KEYS:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            await db_query("INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))

        # Инициализируем базовые настройки бота
        default_settings = [
            ('SAFETY_MODE', 'standard'),
            ('ENABLE_SAFETY_FALLBACK', 'true'),
            ('DEBUG_MODE', 'false'),
            ('LOG_LEVEL', 'INFO'),
            ('LOG_SAFETY_DECISIONS', 'false'),
            ('ENABLE_CACHE', 'true'),
            ('CACHE_TTL_HOURS', '72'),
            ('MAX_RETRIES', '3'),
            ('REQUEST_TIMEOUT_SECONDS', '60'),
            ('ENABLE_PROMPT_SIMPLIFICATION', 'true'),
            ('ENABLE_SYSTEM_INSTRUCTION_FALLBACK', 'true')
        ]

        for setting_name, default_value in default_settings:
            await db_query(
                "INSERT INTO bot_settings (setting_name, value) VALUES ($1, $2) ON CONFLICT (setting_name) DO NOTHING",
                (setting_name, default_value)
            )

        logging.info("Database initialized successfully")

    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")
        raise

async def get_user_chat(user_id: int) -> ChatState:
    result = await db_query("SELECT * FROM chats WHERE user_id = $1", (user_id,))
    if result:
        row = result[0]
        return ChatState(
            history=json.loads(row['history']) if row['history'] else [],
            model=row['model'] or settings.DEFAULT_MODEL,
            token_count=row['token_count'] or 0,
            search_enabled=bool(row['search_enabled']),
            system_prompt=row['system_prompt'] or None
        )
    return ChatState(history=[], model=settings.DEFAULT_MODEL, token_count=0, search_enabled=False, system_prompt=None)

async def update_user_chat(user_id: int, chat_state: ChatState):
    history_json = json.dumps(chat_state.history)
    query = """
    INSERT INTO chats (user_id, history, model, token_count, search_enabled, system_prompt)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (user_id)
    DO UPDATE SET
        history = EXCLUDED.history, model = EXCLUDED.model, token_count = EXCLUDED.token_count,
        search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt;
    """
    await db_query(query, (user_id, history_json, chat_state.model, chat_state.token_count, int(chat_state.search_enabled), chat_state.system_prompt))

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

def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID

async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    result = await db_query("SELECT is_authorized FROM users WHERE user_id = $1", (user_id,))
    return result and result[0]['is_authorized'] == 1

async def _apply_migration_1():
    """Применяет миграцию версии 1: переименование колонки request_count в credit_usage"""
    try:
        # Проверяем, существует ли старая колонка
        check_column_query = "SELECT 1 FROM information_schema.columns WHERE table_name='tavily_key_usage' AND column_name='request_count';"
        column_exists = await db_query(check_column_query)

        if column_exists:
            logging.info("Applying migration 1: renaming 'request_count' to 'credit_usage'...")
            await db_query("ALTER TABLE tavily_key_usage RENAME COLUMN request_count TO credit_usage;")
            logging.info("Migration 1 completed successfully.")
        else:
            logging.info("Migration 1 not needed: column 'request_count' not found.")

    except Exception as e:
        logging.error(f"Error applying migration 1: {e}")
        raise

async def run_migrations():
    """Запускает миграции базы данных"""
    try:
        from .db_migrations import migration_manager
        result = await migration_manager.migrate_up()
        logging.info(f"Database migrations completed: {result}")
        return result
    except Exception as e:
        logging.error(f"Migration error: {e}")
        return {"applied": 0, "status": "failed", "error": str(e)}

async def close_db():
    """Закрывает пул соединений с базой данных"""
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None
        logging.info("Database pool closed")
