import logging
import json
import hashlib
import asyncio
import re
from datetime import datetime, date
import pytz
from app.config import UTC_TZ
import asyncpg
from asyncpg.pool import Pool
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import time

from app.config import settings
from app.utils.time import get_pacific_tz

db_pool: Optional[Pool] = None

# Кэш активных ключей для каждой модели
_active_keys_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = asyncio.Lock()
_cache_last_updated: Dict[str, float] = {}
_cache_ttl = 60  # 1 минута TTL для кэша (было 300)

@dataclass
class ChatState:
    history: List[Dict[str, Any]]
    model: str
    token_count: int
    search_enabled: bool
    system_prompt: Optional[str]
    is_deep_dive: bool = False
    deep_dive_thread_id: Optional[str] = None

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
        
        # Start connection pool monitoring only if pool is valid
        if pool and not pool._closed:
            asyncio.create_task(_monitor_connection_pool(pool))
            logging.info("Database pool monitoring started")
        
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
            # Проверяем, что пул все еще валиден
            if not pool or pool._closed:
                logging.info("Pool is closed or invalid, stopping monitoring")
                break
                
            # Получаем статистику пула с безопасным доступом к атрибутам
            pool_stats = {}
            
            try:
                # Попытка получить статистику через стандартные атрибуты
                if hasattr(pool, '_minsize'):
                    pool_stats['min_size'] = pool._minsize
                if hasattr(pool, '_maxsize'):
                    pool_stats['max_size'] = pool._maxsize
                if hasattr(pool, '_size'):
                    pool_stats['size'] = pool._size
                if hasattr(pool, '_free_size'):
                    pool_stats['free_size'] = pool._free_size
                
                # Вычисляем дополнительные метрики только если есть необходимые данные
                if 'size' in pool_stats and 'free_size' in pool_stats:
                    pool_stats['in_use'] = pool_stats['size'] - pool_stats['free_size']
                    if 'max_size' in pool_stats and pool_stats['max_size'] > 0:
                        pool_stats['utilization'] = (pool_stats['size'] - pool_stats['free_size']) / pool_stats['max_size'] * 100
                    else:
                        pool_stats['utilization'] = 0
                else:
                    pool_stats['in_use'] = 'unknown'
                    pool_stats['utilization'] = 'unknown'
                        
            except AttributeError as attr_error:
                # Если атрибуты недоступны, логируем предупреждение
                logging.info("Some pool attributes not available: %s", attr_error)
                pool_stats['error'] = 'Some pool attributes not accessible'
            
            # Логируем статистику каждые 30 секунд
            logging.info("Database pool stats: %s", pool_stats)
            
            # Предупреждения только если есть валидные данные
            if 'utilization' in pool_stats and isinstance(pool_stats['utilization'], (int, float)):
                if pool_stats['utilization'] > 80:
                    logging.warning("Database pool high utilization: %.1f%%", pool_stats['utilization'])
            
            if 'free_size' in pool_stats and isinstance(pool_stats['free_size'], (int, float)):
                if pool_stats['free_size'] == 0:
                    logging.warning("Database pool exhausted - no free connections available")
                
            await asyncio.sleep(30)  # Проверяем каждые 30 секунд
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            # Логируем ошибку, но не позволяем ей прервать мониторинг
            logging.warning("Connection pool monitoring error (continuing): %s", e)
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
    """
    Выполняет запрос к базе данных с retry логикой и proper error handling.
    
    Args:
        query: SQL запрос
        params: Параметры запроса
        retries: Количество попыток повторного выполнения
        
    Returns:
        Результат запроса
        
    Raises:
        DatabaseConnectionError: При проблемах с подключением
        DatabaseQueryError: При ошибках выполнения запроса
        DatabaseRateLimitError: При превышении лимитов
    """
    # Валидация входных параметров
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must be a non-empty string")
    
    if not isinstance(params, (tuple, list)):
        raise ValueError("Params must be a tuple or list")
    
    if not isinstance(retries, int) or retries < 0:
        raise ValueError("Retries must be a non-negative integer")
    
    last_exception = None
    
    for attempt in range(retries + 1):
        try:
            if not db_pool:
                raise Exception("Database pool not initialized")
            
            if db_pool._closed:
                raise Exception("Database pool is closed")
            
            async with db_pool.acquire() as conn:
                # Выполняем запрос с timeout
                result = await asyncio.wait_for(
                    conn.fetch(query, *params),
                    timeout=30.0  # 30 секунд timeout
                )
                # Convert asyncpg Records to dictionaries for easier handling
                return [dict(record) for record in result]
                
        except asyncio.TimeoutError:
            last_exception = Exception(f"Database query timeout after 30 seconds: {query[:100]}...")
            logging.warning(f"Database query timeout (attempt {attempt + 1}/{retries + 1}): {query[:100]}...")
            
        except Exception as e:
            last_exception = e
            error_msg = str(e).lower()
            
            # Определяем тип ошибки
            if "rate limit" in error_msg or "quota" in error_msg:
                logging.error(f"Database rate limit exceeded: {e}")
                raise Exception(f"Database rate limit exceeded: {e}")
            elif "connection" in error_msg or "timeout" in error_msg:
                logging.warning(f"Database connection issue (attempt {attempt + 1}/{retries + 1}): {e}")
            else:
                logging.error(f"Database query error (attempt {attempt + 1}/{retries + 1}): {e}")
            
            # Если это последняя попытка, выбрасываем исключение
            if attempt == retries:
                break
            
            # Ждем перед повторной попыткой с exponential backoff
            wait_time = min(2 ** attempt, 10)  # Максимум 10 секунд
            logging.info(f"Retrying database query in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
    
    # Если все попытки исчерпаны, выбрасываем последнее исключение
    if last_exception:
        raise last_exception
    else:
        raise Exception("Database query failed after all retries")

async def init_db():
    global db_pool
    if not settings.DATABASE_URL:
        raise Exception("DATABASE_URL not set")
    db_pool = await _create_db_pool()
    
    # Apply Supabase-specific optimizations
    await optimize_database_connections()
    
    await db_query("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0, is_deep_dive BOOLEAN DEFAULT FALSE)""")
    await db_query("""CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)""")
    # --- Roles & Conversations schema ---
    await db_query("""
        CREATE TABLE IF NOT EXISTS roles (
            id SERIAL PRIMARY KEY,
            key TEXT UNIQUE,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            is_default BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS user_roles (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            role_type TEXT NULL,
            role_id INTEGER NULL,
            summary TEXT NULL,
            token_budget BIGINT NULL,
            archived BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            token_estimate BIGINT DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Useful indexes
    await db_query("CREATE INDEX IF NOT EXISTS idx_conversations_user_updated ON conversations(user_id, updated_at DESC)")
    await db_query("CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON conversation_messages(conversation_id, created_at)")
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

    # Настройка RLS (Row Level Security)
    await setup_row_level_security()

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
        
        # --- Users Table Migration (deep_dive_thread_id) ---
        if 'deep_dive_thread_id' not in {c['column_name'] for c in users_columns}:
            logging.info("Column 'deep_dive_thread_id' not found in 'users' table. Attempting schema migration...")
            await db_query("ALTER TABLE users ADD COLUMN deep_dive_thread_id TEXT;")
            logging.info("Schema migration for 'deep_dive_thread_id' successful.")

    except asyncpg.PostgresError as e:
        logging.warning(f"A schema migration may have been skipped or failed: {e}")
    
    await db_query("INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING", (settings.ADMIN_ID,))
    for key in settings.GEMINI_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))
    for key in settings.TAVILY_API_KEYS:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        await db_query("INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING", (key_hash, key))

async def setup_row_level_security():
    """Настраивает Row Level Security для всех таблиц"""
    try:
        # Включаем RLS для всех таблиц
        tables_with_rls = [
            'users',
            'chats', 
            'roles',
            'user_roles',
            'conversations',
            'conversation_messages',
            'user_documents',
            'api_keys',
            'key_usage',
            'tavily_api_keys',
            'tavily_key_usage',
            'group_chats',
            'group_members',
            'group_messages',
            'metrics',
            'error_logs'
        ]
        
        for table in tables_with_rls:
            try:
                # Включаем RLS
                await db_query(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
                logging.info(f"RLS enabled for table: {table}")
                
                # Создаем политики безопасности
                await create_rls_policies(table)
                
            except Exception as e:
                logging.warning(f"Failed to enable RLS for table {table}: {e}")
        
        logging.info("Row Level Security setup completed")
        
    except Exception as e:
        logging.error(f"Error setting up RLS: {e}")

async def create_rls_policies(table_name: str):
    """Создает политики безопасности для таблицы"""
    try:
        if table_name == 'users':
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'users' AND policyname = 'users_policy'
                """)
                
                if not existing_policy:
                    # Создаем единую политику для всех операций с пользователями
                    await db_query("""
                        CREATE POLICY users_policy ON users
                        FOR ALL USING (
                            user_id = (SELECT current_setting('app.user_id', true)::bigint) OR 
                            (SELECT current_setting('app.is_admin', true)::boolean = true)
                        );
                    """)
                    logging.info(f"Created users_policy for table: {table_name}")
                else:
                    logging.info(f"Policy users_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create users_policy: {e}")
                raise e
                
                # Создаем индекс для оптимизации RLS политики
                try:
                    await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_users_user_id_rls ON users(user_id)")
                    logging.info(f"Created index for RLS optimization on {table_name}")
                except Exception as e:
                    logging.warning(f"Could not create index for {table_name}: {e}")
                    
            except Exception as e:
                logging.error(f"Failed to create users_policy: {e}")
                raise e
            
        elif table_name == 'chats':
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'chats' AND policyname = 'chats_policy'
                """)
                
                if not existing_policy:
                    # Пользователи могут читать/изменять только свои чаты
                    await db_query("""
                        CREATE POLICY chats_policy ON chats
                        FOR ALL USING (
                            user_id = (SELECT current_setting('app.user_id', true)::bigint) OR 
                            (SELECT current_setting('app.is_admin', true)::boolean = true)
                        );
                    """)
                    logging.info(f"Created chats_policy for table: {table_name}")
                else:
                    logging.info(f"Policy chats_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create chats_policy: {e}")
                raise e
                
                # Создаем составной индекс для оптимизации RLS политики
                try:
                    await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chats_user_id_admin_rls ON chats(user_id)")
                    logging.info(f"Created composite index for RLS optimization on {table_name}")
                except Exception as e:
                    logging.warning(f"Could not create composite index for {table_name}: {e}")
                
                # Создаем индекс для оптимизации RLS политики
                try:
                    await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chats_user_id_rls ON chats(user_id)")
                    logging.info(f"Created index for RLS optimization on {table_name}")
                except Exception as e:
                    logging.warning(f"Could not create index for {table_name}: {e}")
                    
            except Exception as e:
                logging.error(f"Failed to create chats_policy: {e}")
                raise e
            
        elif table_name == 'user_documents':
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'user_documents' AND policyname = 'user_documents_policy'
                """)
                
                if not existing_policy:
                    # Пользователи могут читать/изменять только свои документы
                    await db_query("""
                        CREATE POLICY user_documents_policy ON user_documents
                        FOR ALL USING (
                            user_id = (SELECT current_setting('app.user_id', true)::bigint) OR 
                            (SELECT current_setting('app.is_admin', true)::boolean = true)
                        );
                    """)
                    logging.info(f"Created user_documents_policy for table: {table_name}")
                else:
                    logging.info(f"Policy user_documents_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create user_documents_policy: {e}")
                raise e
                
                # Создаем индекс для оптимизации RLS политики
                try:
                    await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_documents_user_id_rls ON user_documents(user_id)")
                    logging.info(f"Created index for RLS optimization on {table_name}")
                except Exception as e:
                    logging.warning(f"Could not create index for {table_name}: {e}")
                    
            except Exception as e:
                logging.error(f"Failed to create user_documents_policy: {e}")
                raise e
            
        elif table_name == 'roles':
            # Предустановленные роли доступны на чтение всем, изменение только админом
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'roles' AND policyname = 'roles_read_policy'
                """)
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY roles_read_policy ON roles
                        FOR SELECT USING (true);
                    """)
                existing_write = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'roles' AND policyname = 'roles_write_policy'
                """)
                if not existing_write:
                    await db_query("""
                        CREATE POLICY roles_write_policy ON roles
                        FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));
                    """)
            except Exception as e:
                logging.error(f"Failed to create roles policies: {e}")
                raise e

        elif table_name == 'user_roles':
            # Пользователь видит/меняет только свои пользовательские роли
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'user_roles' AND policyname = 'user_roles_policy'
                """
                )
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY user_roles_policy ON user_roles
                        FOR ALL USING (
                            user_id = (SELECT current_setting('app.user_id', true)::bigint) OR 
                            (SELECT current_setting('app.is_admin', true)::boolean = true)
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create user_roles policy: {e}")
                raise e

        elif table_name == 'conversations':
            # Пользователь видит/меняет только свои беседы
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'conversations' AND policyname = 'conversations_policy'
                """)
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY conversations_policy ON conversations
                        FOR ALL USING (
                            user_id = (SELECT current_setting('app.user_id', true)::bigint) OR 
                            (SELECT current_setting('app.is_admin', true)::boolean = true)
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create conversations policy: {e}")
                raise e

        elif table_name == 'conversation_messages':
            # Доступ к сообщениям лишь если владеешь соответствующей беседой
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'conversation_messages' AND policyname = 'conversation_messages_policy'
                """)
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY conversation_messages_policy ON conversation_messages
                        FOR ALL USING (
                            (SELECT current_setting('app.is_admin', true)::boolean = true)
                            OR EXISTS (
                                SELECT 1 FROM conversations c 
                                WHERE c.id = conversation_messages.conversation_id
                                  AND c.user_id = (SELECT current_setting('app.user_id', true)::bigint)
                            )
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create conversation_messages policy: {e}")
                raise e

        elif table_name in ['api_keys', 'tavily_api_keys']:
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = $1 AND policyname = $2
                """, (table_name, f"{table_name}_policy"))
                
                if not existing_policy:
                    # Только админы могут работать с API ключами
                    await db_query(f"""
                        CREATE POLICY {table_name}_policy ON {table_name}
                        FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));
                    """)
                    logging.info(f"Created {table_name}_policy for table: {table_name}")
                else:
                    logging.info(f"Policy {table_name}_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e
            
        elif table_name in ['key_usage', 'tavily_key_usage']:
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = $1 AND policyname = $2
                """, (table_name, f"{table_name}_policy"))
                
                if not existing_policy:
                    # Только админы могут читать статистику использования
                    await db_query(f"""
                        CREATE POLICY {table_name}_policy ON {table_name}
                        FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));
                    """)
                    logging.info(f"Created {table_name}_policy for table: {table_name}")
                else:
                    logging.info(f"Policy {table_name}_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e
            
        elif table_name in ['group_chats', 'group_members', 'group_messages']:
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = $1 AND policyname = $2
                """, (table_name, f"{table_name}_policy"))
                
                if not existing_policy:
                    # Пользователи могут работать только с группами, где они участники
                    await db_query(f"""
                        CREATE POLICY {table_name}_policy ON {table_name}
                        FOR ALL USING (
                            (SELECT current_setting('app.is_admin', true)::boolean = true) OR
                            EXISTS (
                                SELECT 1 FROM group_members gm 
                                WHERE gm.chat_id = {table_name}.chat_id 
                                AND gm.user_id = (SELECT current_setting('app.user_id', true)::bigint)
                            )
                        );
                    """)
                    logging.info(f"Created {table_name}_policy for table: {table_name}")
                else:
                    logging.info(f"Policy {table_name}_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e
                
                # Создаем индексы для оптимизации RLS политики
                try:
                    if table_name == 'group_chats':
                        await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_group_chats_chat_id_rls ON group_chats(chat_id)")
                    elif table_name == 'group_members':
                        await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_group_members_chat_user_rls ON group_members(chat_id, user_id)")
                    elif table_name == 'group_messages':
                        await db_query("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_group_messages_chat_id_rls ON group_messages(chat_id)")
                    logging.info(f"Created indexes for RLS optimization on {table_name}")
                except Exception as e:
                    logging.warning(f"Could not create indexes for {table_name}: {e}")
                    
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e
            
        elif table_name in ['metrics', 'error_logs']:
            # Проверяем существование политики перед созданием
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = $1 AND policyname = $2
                """, (table_name, f"{table_name}_policy"))
                
                if not existing_policy:
                    # Только админы могут читать метрики и логи
                    await db_query(f"""
                        CREATE POLICY {table_name}_policy ON {table_name}
                        FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));
                    """)
                    logging.info(f"Created {table_name}_policy for table: {table_name}")
                else:
                    logging.info(f"Policy {table_name}_policy already exists for table: {table_name}")
                    
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e
            
        logging.info(f"RLS policies created for table: {table_name}")
        
    except Exception as e:
        logging.error(f"Error creating RLS policies for {table_name}: {e}")

async def set_user_context(user_id: int, is_admin: bool = False):
    """Устанавливает контекст пользователя для RLS"""
    try:
        await db_query("SELECT set_config('app.user_id', $1, false)", (str(user_id),))
        await db_query("SELECT set_config('app.is_admin', $1, false)", (str(is_admin).lower(),))
        logging.debug(f"User context set: user_id={user_id}, is_admin={is_admin}")
    except Exception as e:
        logging.warning(f"Failed to set user context: {e}")

async def clear_user_context():
    """Очищает контекст пользователя"""
    try:
        await db_query("SELECT set_config('app.user_id', '', false)")
        await db_query("SELECT set_config('app.is_admin', 'false', false)")
    except Exception as e:
        logging.warning(f"Failed to clear user context: {e}")

async def get_user_chat(user_id: int) -> ChatState:
    # Устанавливаем контекст пользователя для RLS
    await set_user_context(user_id, is_admin(user_id))
    
    try:
        chat_result = await db_query("SELECT * FROM chats WHERE user_id = $1", (user_id,))
        user_result = await db_query("SELECT is_deep_dive, deep_dive_thread_id FROM users WHERE user_id = $1", (user_id,))

        chat_state = ChatState(history=[], model=settings.DEFAULT_MODEL, token_count=0, search_enabled=False, system_prompt=None, is_deep_dive=False, deep_dive_thread_id=None)

        if chat_result:
            row = chat_result[0]
            chat_state.history = json.loads(row['history']) if row['history'] else []
            chat_state.model = row['model'] or settings.DEFAULT_MODEL
            chat_state.token_count = row['token_count'] or 0
            chat_state.search_enabled = bool(row['search_enabled'])
            chat_state.system_prompt = row['system_prompt'] or None

        if user_result:
            chat_state.is_deep_dive = user_result[0]['is_deep_dive'] or False
            chat_state.deep_dive_thread_id = user_result[0].get('deep_dive_thread_id')
            
        return chat_state
    finally:
        # Очищаем контекст пользователя
        await clear_user_context()

async def update_user_chat(user_id: int, chat_state: ChatState):
    # Устанавливаем контекст пользователя для RLS
    await set_user_context(user_id, is_admin(user_id))
    
    try:
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

        user_query = "UPDATE users SET is_deep_dive = $1, deep_dive_thread_id = $2 WHERE user_id = $3"
        await db_query(user_query, (chat_state.is_deep_dive, chat_state.deep_dive_thread_id, user_id))
    finally:
        # Очищаем контекст пользователя
        await clear_user_context()

async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    """
    Получает доступный ключ Gemini API для модели.
    Использует кэширование для оптимизации и стратегию "один ключ до исчерпания лимита".
    
    Args:
        model_name: Название модели (gemini-2.5-flash, gemini-2.5-pro, etc.)
        
    Returns:
        Dict с key_hash и api_key или None если все ключи исчерпаны
    """
    # Валидация входных параметров
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("model_name must be a non-empty string")
    
    # Устанавливаем админский контекст для работы с API ключами
    await set_user_context(settings.ADMIN_ID, True)
    
    try:
        async with _cache_lock:
            # Проверяем кэш
            if model_name in _active_keys_cache:
                cached_key = _active_keys_cache[model_name]
                # Проверяем, не исчерпан ли кэшированный ключ
                if await _is_key_available(cached_key['key_hash'], model_name):
                    return cached_key
                else:
                    # Ключ исчерпан, удаляем из кэша
                    del _active_keys_cache[model_name]
                    if model_name in _cache_last_updated:
                        del _cache_last_updated[model_name]
            
            # Если кэш пуст или ключ исчерпан, получаем новый
            new_key = await _get_fresh_available_key(model_name)
            if new_key:
                _active_keys_cache[model_name] = new_key
                _cache_last_updated[model_name] = time.time()
            
            return new_key
    finally:
        # Очищаем контекст пользователя
        await clear_user_context()

async def _is_key_available(key_hash: str, model_name: str) -> bool:
    """
    Проверяет, доступен ли ключ для использования.
    
    Args:
        key_hash: Хэш ключа
        model_name: Название модели
        
    Returns:
        True если ключ доступен, False если исчерпан
    """
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    
    if not daily_limit:
        return True
    
    query = """
        SELECT COALESCE(request_count, 0) as request_count
        FROM key_usage 
        WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3
    """
    
    result = await db_query(query, (key_hash, model_name, today_pacific))
    current_usage = result[0]['request_count'] if result else 0
    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
    
    return current_usage < threshold

async def _get_fresh_available_key(model_name: str) -> Optional[Dict[str, Any]]:
    """
    Получает свежий доступный ключ из базы данных.
    
    Args:
        model_name: Название модели
        
    Returns:
        Dict с key_hash и api_key или None если все ключи исчерпаны
    """
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    
    if not daily_limit:
        # Если нет лимита для модели, берем первый доступный ключ
        keys = await db_query("SELECT * FROM api_keys LIMIT 1")
        return keys[0] if keys else None
    
    # Получаем все ключи с их текущим использованием для данной модели
    query = """
        SELECT ak.key_hash, ak.api_key, COALESCE(ku.request_count, 0) as request_count
        FROM api_keys ak
        LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash 
            AND ku.model_name = $1 AND ku.usage_date = $2
        ORDER BY COALESCE(ku.request_count, 0) ASC
    """
    
    results = await db_query(query, (model_name, today_pacific))
    
    if not results:
        return None
    
    # Ищем ключ, который еще не достиг лимита
    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
    
    for row in results:
        if row['request_count'] < threshold:
            return {
                'key_hash': row['key_hash'],
                'api_key': row['api_key']
            }
    
    # Если все ключи достигли лимита, возвращаем None
    return None

async def invalidate_key_cache(model_name: str = None):
    """
    Инвалидирует кэш ключей.
    
    Args:
        model_name: Название модели для инвалидации (None для всех моделей)
    """
    async with _cache_lock:
        if model_name:
            if model_name in _active_keys_cache:
                del _active_keys_cache[model_name]
            if model_name in _cache_last_updated:
                del _cache_last_updated[model_name]
        else:
            _active_keys_cache.clear()
            _cache_last_updated.clear()

async def get_current_active_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    """
    Получает текущий активный ключ для модели.
    Если нет активного ключа, выбирает новый с наименьшим использованием.
    
    Args:
        model_name: Название модели
        
    Returns:
        Dict с key_hash и api_key или None если все ключи исчерпаны
    """
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    
    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys LIMIT 1")
        return keys[0] if keys else None
    
    # Сначала проверяем, есть ли уже активный ключ для этой модели
    active_key_query = """
        SELECT ak.key_hash, ak.api_key, COALESCE(ku.request_count, 0) as request_count
        FROM api_keys ak
        LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash 
            AND ku.model_name = $1 AND ku.usage_date = $2
        WHERE COALESCE(ku.request_count, 0) < $3
        ORDER BY COALESCE(ku.request_count, 0) ASC
        LIMIT 1
    """
    
    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
    results = await db_query(active_key_query, (model_name, today_pacific, threshold))
    
    if results:
        return {
            'key_hash': results[0]['key_hash'],
            'api_key': results[0]['api_key']
        }
    
    return None

async def get_next_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    """
    Получает следующий доступный ключ для модели.
    Используется когда текущий ключ исчерпан.
    
    Args:
        model_name: Название модели
        
    Returns:
        Dict с key_hash и api_key или None если все ключи исчерпаны
    """
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    
    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys LIMIT 1")
        return keys[0] if keys else None
    
    # Получаем ключ с наименьшим использованием
    query = """
        SELECT ak.key_hash, ak.api_key, COALESCE(ku.request_count, 0) as request_count
        FROM api_keys ak
        LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash 
            AND ku.model_name = $1 AND ku.usage_date = $2
        ORDER BY COALESCE(ku.request_count, 0) ASC
        LIMIT 1
    """
    
    results = await db_query(query, (model_name, today_pacific))
    
    if not results:
        return None
    
    row = results[0]
    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
    
    # Проверяем, не достиг ли ключ лимита
    if row['request_count'] >= threshold:
        return None
    
    return {
        'key_hash': row['key_hash'],
        'api_key': row['api_key']
    }

async def increment_gemini_key_usage(key_hash: str, model_name: str):
    """
    Инкрементирует счетчик использования ключа и инвалидирует кэш при необходимости.
    
    Args:
        key_hash: Хэш ключа
        model_name: Название модели
    """
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    
    # Инкрементируем использование
    query = """
        INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, 1)
        ON CONFLICT (key_hash, model_name, usage_date)
        DO UPDATE SET request_count = key_usage.request_count + 1;
    """
    await db_query(query, (key_hash, model_name, today_pacific))
    
    # Проверяем, не достиг ли ключ лимита
    daily_limit = settings.DAILY_LIMITS.get(model_name)
    if daily_limit:
        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
        
        # Получаем текущее использование
        usage_query = """
            SELECT request_count FROM key_usage 
            WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3
        """
        result = await db_query(usage_query, (key_hash, model_name, today_pacific))
        current_usage = result[0]['request_count'] if result else 0
        
        # Если достигли лимита, инвалидируем кэш для этой модели
        if current_usage >= threshold:
            await invalidate_key_cache(model_name)
            logging.info(f"Key {key_hash[:8]}... reached limit for model {model_name}. Cache invalidated.")
        else:
            # Если ключ еще не достиг лимита, обновляем время последнего использования
            async with _cache_lock:
                if model_name in _cache_last_updated:
                    _cache_last_updated[model_name] = time.time()

async def get_available_tavily_key():
    current_month = datetime.now(UTC_TZ).strftime('%Y-%m')
    
    # Оптимизированный запрос: получаем все данные одним запросом
    query = """
        SELECT tak.key_hash, tak.api_key, COALESCE(tku.credit_usage, 0) as credit_usage
        FROM tavily_api_keys tak
        LEFT JOIN tavily_key_usage tku ON tak.key_hash = tku.key_hash 
            AND tku.usage_month = $1
        ORDER BY COALESCE(tku.credit_usage, 0) ASC
    """
    
    results = await db_query(query, (current_month,))
    threshold = settings.TAVILY_MONTHLY_CREDIT_LIMIT * settings.TAVILY_LIMIT_THRESHOLD_PERCENT
    
    for row in results:
        if row['credit_usage'] < threshold:
            return {
                'key_hash': row['key_hash'],
                'api_key': row['api_key']
            }
    
    return None

async def increment_tavily_key_usage(key_hash: str, cost: int):
    current_month = datetime.now(UTC_TZ).strftime('%Y-%m')
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

async def get_gemini_key_usage_stats(model_name: str = None) -> List[Dict[str, Any]]:
    """
    Получает статистику использования ключей Gemini API.
    
    Args:
        model_name: Название модели (None для всех моделей)
        
    Returns:
        Список словарей с информацией об использовании ключей
    """
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    
    if model_name:
        # Статистика для конкретной модели
        query = """
            SELECT 
                ak.key_hash,
                LEFT(ak.api_key, 10) || '...' as api_key_preview,
                COALESCE(ku.request_count, 0) as request_count,
                $2 as daily_limit,
                CASE 
                    WHEN $2 IS NULL THEN 0
                    ELSE (COALESCE(ku.request_count, 0)::float / $2 * 100)
                END as usage_percent,
                CASE 
                    WHEN $2 IS NULL THEN true
                    ELSE COALESCE(ku.request_count, 0) < ($2 * $3)
                END as is_available
            FROM api_keys ak
            LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash 
                AND ku.model_name = $1 AND ku.usage_date = $4
            ORDER BY COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(query, (model_name, settings.DAILY_LIMITS.get(model_name), settings.LIMIT_THRESHOLD_PERCENT, today_pacific))
    else:
        # Статистика для всех моделей
        query = """
            SELECT 
                ak.key_hash,
                LEFT(ak.api_key, 10) || '...' as api_key_preview,
                ku.model_name,
                COALESCE(ku.request_count, 0) as request_count,
                CASE 
                    WHEN ku.model_name = 'gemini-2.5-flash' THEN 250
                    WHEN ku.model_name = 'gemini-2.5-pro' THEN 100
                    WHEN ku.model_name = 'gemini-2.5-flash-lite' THEN 1000
                    ELSE NULL
                END as daily_limit,
                CASE 
                    WHEN ku.model_name = 'gemini-2.5-flash' THEN (COALESCE(ku.request_count, 0)::float / 250 * 100)
                    WHEN ku.model_name = 'gemini-2.5-pro' THEN (COALESCE(ku.request_count, 0)::float / 100 * 100)
                    WHEN ku.model_name = 'gemini-2.5-flash-lite' THEN (COALESCE(ku.request_count, 0)::float / 1000 * 100)
                    ELSE 0
                END as usage_percent,
                CASE 
                    WHEN ku.model_name = 'gemini-2.5-flash' THEN COALESCE(ku.request_count, 0) < (250 * $1)
                    WHEN ku.model_name = 'gemini-2.5-pro' THEN COALESCE(ku.request_count, 0) < (100 * $1)
                    WHEN ku.model_name = 'gemini-2.5-flash-lite' THEN COALESCE(ku.request_count, 0) < (1000 * $1)
                    ELSE true
                END as is_available
            FROM api_keys ak
            LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash AND ku.usage_date = $2
            ORDER BY ku.model_name, COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(query, (settings.LIMIT_THRESHOLD_PERCENT, today_pacific))
    
    return results

async def get_active_key_info(model_name: str) -> Optional[Dict[str, Any]]:
    """
    Получает информацию о текущем активном ключе для модели.
    
    Args:
        model_name: Название модели
        
    Returns:
        Информация об активном ключе или None
    """
    async with _cache_lock:
        if model_name in _active_keys_cache:
            cached_key = _active_keys_cache[model_name]
            is_available = await _is_key_available(cached_key['key_hash'], model_name)
            
            return {
                'key_hash': cached_key['key_hash'],
                'api_key_preview': cached_key['api_key'][:10] + '...',
                'is_available': is_available,
                'cached_at': _cache_last_updated.get(model_name, 0)
            }
    
    return None

async def force_update_tavily_keys():
    """Принудительно обновляет ключи Tavily из настроек"""
    try:
        from app.config import get_settings
        
        settings = get_settings()
        if not settings or not settings.TAVILY_API_KEYS:
            logging.error("TAVILY_API_KEYS not found in settings")
            return False
        
        # Очищаем старые ключи
        await db_query("DELETE FROM tavily_api_keys")
        logging.info("Old Tavily API keys cleared")
        
        # Добавляем новые ключи
        for key in settings.TAVILY_API_KEYS:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            await db_query(
                "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2)",
                (key_hash, key)
            )
            logging.info(f"Added new Tavily API key: {key[:10]}...")
        
        # Очищаем старые записи использования
        await db_query("DELETE FROM tavily_key_usage")
        logging.info("Old Tavily key usage records cleared")
        
        # Очищаем кэш активных ключей
        global _active_keys_cache
        async with _cache_lock:
            _active_keys_cache.clear()
            _cache_last_updated.clear()
        
        logging.info("Tavily API keys updated successfully")
        return True
        
    except Exception as e:
        logging.error(f"Failed to update Tavily API keys: {e}")
        return False

def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID

async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    
    # Устанавливаем контекст пользователя для RLS
    await set_user_context(user_id, False)
    
    try:
        result = await db_query("SELECT is_authorized FROM users WHERE user_id = $1", (user_id,))
        return result and result[0]['is_authorized'] == 1
    finally:
        # Очищаем контекст пользователя
        await clear_user_context()

# ============================================================================
# CONVERSATION MANAGEMENT
# ============================================================================

async def save_conversation(user_id: int, title: str, role_type: str = None, role_id: int = None) -> int:
    """Сохранить текущую беседу пользователя"""
    try:
        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return None
            
        # Создаём беседу
        result = await db_query(
            """INSERT INTO conversations (user_id, title, role_type, role_id, summary, token_budget, created_at) 
               VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP) RETURNING id""",
            (user_id, title, role_type, role_id, None, chat_state.token_count)
        )
        conv_id = result[0]['id'] if result else None
        
        if conv_id:
            # Сохраняем сообщения из истории
            if chat_state.history:
                import json
                try:
                    # chat_state.history может быть уже списком или JSON строкой
                    if isinstance(chat_state.history, list):
                        history_data = {'messages': chat_state.history}
                    else:
                        history_data = json.loads(chat_state.history)
                    
                    for msg in history_data.get('messages', []):
                        # Обрабатываем разные форматы сообщений
                        if isinstance(msg, dict):
                            role = msg.get('role', 'user')
                            content = msg.get('content', '')
                            if isinstance(content, list):
                                # Если content это список parts, объединяем
                                content = ' '.join(str(part) for part in content)
                            # Фильтруем технические/системные сообщения и команды
                            text_lower = (content or '').strip()
                            if role not in ('user', 'assistant'):
                                continue
                            if text_lower.startswith('/'):
                                continue
                            # Фильтруем наши служебные тексты-индикаторы
                            if any(prefix in text_lower for prefix in (
                                '🖼️ обрабатываю изображение',
                                '🤔 думаю',
                                '📄 обрабатываю документ',
                                '✅ новый чат создан',
                                'опишите, какую роль хотите создать',
                                'не удалось сгенерировать роль',
                                'сервер перегружен'
                            )):
                                continue
                        else:
                            # Если msg не словарь, используем как есть
                            role = 'user'
                            content = str(msg)
                        
                        await db_query(
                            """INSERT INTO conversation_messages (conversation_id, role, content, created_at) 
                               VALUES ($1, $2, $3, CURRENT_TIMESTAMP)""",
                            (conv_id, role, content)
                        )
                except (json.JSONDecodeError, TypeError) as e:
                    logging.warning(f"Failed to parse history for conversation {conv_id}: {e}")
        
        return conv_id
    except Exception as e:
        logging.error(f"Error saving conversation for user {user_id}: {e}")
        return None

async def get_user_conversations(user_id: int, limit: int = 10, offset: int = 0) -> list:
    """Получить список бесед пользователя с пагинацией"""
    try:
        result = await db_query(
            """SELECT c.id, c.title, c.role_type, c.role_id, c.summary, c.token_budget, c.created_at,
                      r.title as role_title, ur.title as user_role_title
               FROM conversations c
               LEFT JOIN roles r ON c.role_type = 'role' AND c.role_id = r.id
               LEFT JOIN user_roles ur ON c.role_type = 'user_role' AND c.role_id = ur.id
               WHERE c.user_id = $1 
               ORDER BY c.created_at DESC 
               LIMIT $2 OFFSET $3""",
            (user_id, limit, offset)
        )
        
        conversations = []
        for row in result:
            conversations.append({
                'id': row['id'],
                'title': row['title'],
                'role_type': row['role_type'],
                'role_id': row['role_id'],
                'summary': row['summary'],
                'token_budget': row['token_budget'],
                'created_at': row['created_at'],
                'role_title': row['role_title'] or row['user_role_title']
            })
        return conversations
    except Exception as e:
        logging.error(f"Error getting conversations for user {user_id}: {e}")
        return []

async def get_conversation_messages(conversation_id: int, user_id: int) -> list:
    """Получить сообщения беседы (с проверкой принадлежности пользователю)"""
    try:
        # Проверяем принадлежность беседы пользователю
        conv_check = await db_query(
            "SELECT id FROM conversations WHERE id = $1 AND user_id = $2",
            (conversation_id, user_id)
        )
        if not conv_check:
            return None
            
        result = await db_query(
            """SELECT role, content, created_at 
               FROM conversation_messages 
               WHERE conversation_id = $1 
               ORDER BY created_at ASC""",
            (conversation_id,)
        )
        
        messages = []
        for row in result:
            messages.append({
                'role': row['role'],
                'content': row['content'],
                'created_at': row['created_at']
            })
        return messages
    except Exception as e:
        logging.error(f"Error getting conversation messages for {conversation_id}: {e}")
        return None

async def switch_to_conversation(user_id: int, conversation_id: int) -> bool:
    """Переключиться на беседу (загрузить её в текущий чат)"""
    try:
        # Получаем беседу и проверяем принадлежность
        conv_data = await db_query(
            "SELECT role_type, role_id, summary FROM conversations WHERE id = $1 AND user_id = $2",
            (conversation_id, user_id)
        )
        if not conv_data:
            return False
            
        role_type, role_id, summary = conv_data[0]['role_type'], conv_data[0]['role_id'], conv_data[0]['summary']
        
        # Получаем сообщения беседы
        messages = await get_conversation_messages(conversation_id, user_id)
        if messages is None:
            return False
            
        # Формируем историю в формате JSON
        import json
        history_data = {
            'messages': messages,
            'conversation_id': conversation_id,
            'summary': summary
        }
        history_json = json.dumps(history_data, ensure_ascii=False)
        
        # Обновляем текущий чат
        await db_query(
            "UPDATE chats SET history = $1, token_count = 0 WHERE user_id = $2",
            (history_json, user_id)
        )
        
        # Если есть роль, применяем её
        if role_type and role_id:
            if role_type == 'role':
                role_data = await db_query("SELECT prompt FROM roles WHERE id = $1", (role_id,))
            elif role_type == 'user_role':
                role_data = await db_query("SELECT prompt FROM user_roles WHERE id = $1", (role_id,))
            else:
                role_data = None
                
            if role_data:
                await db_query(
                    "UPDATE chats SET system_prompt = $1 WHERE user_id = $2",
                    (role_data[0]['prompt'], user_id)
                )
        
        return True
    except Exception as e:
        logging.error(f"Error switching to conversation {conversation_id} for user {user_id}: {e}")
        return False

async def rename_conversation(user_id: int, conversation_id: int, new_title: str) -> bool:
    """Переименовать беседу"""
    try:
        result = await db_query(
            "UPDATE conversations SET title = $1 WHERE id = $2 AND user_id = $3",
            (new_title, conversation_id, user_id)
        )
        return result is not None
    except Exception as e:
        logging.error(f"Error renaming conversation {conversation_id} for user {user_id}: {e}")
        return False

async def delete_conversation(user_id: int, conversation_id: int) -> bool:
    """Удалить беседу и все её сообщения"""
    try:
        # Проверяем принадлежность
        conv_check = await db_query(
            "SELECT id FROM conversations WHERE id = $1 AND user_id = $2",
            (conversation_id, user_id)
        )
        if not conv_check:
            return False
            
        # Удаляем сообщения
        await db_query("DELETE FROM conversation_messages WHERE conversation_id = $1", (conversation_id,))
        
        # Удаляем беседу
        await db_query("DELETE FROM conversations WHERE id = $1 AND user_id = $2", (conversation_id, user_id))
        
        return True
    except Exception as e:
        logging.error(f"Error deleting conversation {conversation_id} for user {user_id}: {e}")
        return False

async def get_conversation_count(user_id: int) -> int:
    """Получить общее количество бесед пользователя"""
    try:
        result = await db_query("SELECT COUNT(*) FROM conversations WHERE user_id = $1", (user_id,))
        return result[0]['count'] if result else 0
    except Exception as e:
        logging.error(f"Error getting conversation count for user {user_id}: {e}")
        return 0
