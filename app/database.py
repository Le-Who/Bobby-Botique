import logging
import json
import hashlib
import asyncio
from datetime import datetime, date
from app.config import UTC_TZ, settings
from app.utils.time import get_pacific_tz
import asyncpg
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import time


@dataclass
class ChatState:
    history: List[Dict[str, Any]]
    model: str
    token_count: int
    search_enabled: bool
    system_prompt: Optional[str]
    is_deep_dive: bool = False
    deep_dive_thread_id: Optional[str] = None


class DatabaseManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance.pool = None

            from cachetools import TTLCache

            # TTL Caches to avoid manual background cleanup
            cls._instance._active_keys_cache = TTLCache(maxsize=100, ttl=300)
            cls._instance._user_auth_cache = TTLCache(maxsize=1000, ttl=300)

            cls._instance._cache_lock = asyncio.Lock()
            cls._instance._monitor_task = None
            cls._instance._cleanup_cache_task = None
        return cls._instance

    @property
    def is_connected(self):
        return bool(self.pool and not self.pool._closed)

    async def create_pool(self):
        """Создает пул соединений с базой данных"""
        try:
            self.pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=30,
                statement_cache_size=0,
                max_cached_statement_lifetime=300,
                max_cacheable_statement_size=15000,
                server_settings={
                    "application_name": "gemaibotv2",
                    "tcp_keepalives_idle": "30",
                    "tcp_keepalives_interval": "10",
                    "tcp_keepalives_count": "5",
                    "jit": "off",
                },
            )

            # Sync global variable for backward compatibility
            global db_pool
            db_pool = self.pool

            if self.pool and not self.pool._closed:
                self._monitor_task = self._start_background_task(
                    self._monitor_task,
                    self.monitor_connection_pool,
                    "database pool monitor",
                )
                logging.info("Database pool monitoring started")
                return self.pool
        except Exception as e:
            if "rate limit" in str(e).lower() or "quota" in str(e).lower():
                logging.critical(
                    "Supabase.com rate limit exceeded. Please upgrade your plan or wait for quota reset."
                )
                raise Exception(f"Database rate limit exceeded: {e}")
            elif "connection" in str(e).lower() or "timeout" in str(e).lower():
                logging.warning(
                    "Database connection issue: %s. This might be temporary.", e
                )
                raise Exception(f"Database connection failed: {e}")
            else:
                logging.error("Unexpected database error: %s", e)
                raise Exception(f"Database initialization failed: {e}")

    async def close(self):
        # Cancel background tasks
        await self._cancel_background_task("_monitor_task")
        await self._cancel_background_task("_cleanup_cache_task")

        if self.pool:
            await self.pool.close()
            self.pool = None
            logging.info("Database pool closed")

            # Sync global variable
            global db_pool
            db_pool = None

    async def reconnect(self):
        logging.info("Attempting to reconnect to database...")
        # Close existing pool and task
        await self.close()

        await self.create_pool()
        logging.info("Database reconnected successfully")
        return True

    def _start_background_task(self, task_ref, coro_factory, task_name: str):
        """Запускает фоновую задачу с защитой от повторного старта."""
        if task_ref and not task_ref.done():
            logging.debug("Background task '%s' already running", task_name)
            return task_ref

        return asyncio.create_task(coro_factory())

    async def _cancel_background_task(self, attr_name: str):
        """Отменяет и ожидает завершение фоновой задачи по имени атрибута."""
        task = getattr(self, attr_name, None)
        if not task:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        setattr(self, attr_name, None)

    async def start_cleanup_task(self):
        """Deprecated: TTLCache handles eviction automatically."""
        pass

    async def monitor_connection_pool(self):
        while True:
            try:
                if not self.pool or self.pool._closed:
                    logging.info("Pool is closed or invalid, stopping monitoring")
                    break

                pool_stats = {}
                try:
                    if hasattr(self.pool, "_minsize"):
                        pool_stats["min_size"] = self.pool._minsize
                    if hasattr(self.pool, "_maxsize"):
                        pool_stats["max_size"] = self.pool._maxsize
                    if hasattr(self.pool, "_size"):
                        pool_stats["size"] = self.pool._size
                    if hasattr(self.pool, "_free_size"):
                        pool_stats["free_size"] = self.pool._free_size

                    if "size" in pool_stats and "free_size" in pool_stats:
                        pool_stats["in_use"] = (
                            pool_stats["size"] - pool_stats["free_size"]
                        )
                        if "max_size" in pool_stats and pool_stats["max_size"] > 0:
                            pool_stats["utilization"] = (
                                (pool_stats["size"] - pool_stats["free_size"])
                                / pool_stats["max_size"]
                                * 100
                            )
                        else:
                            pool_stats["utilization"] = 0
                except AttributeError as e:
                    pool_stats["error"] = str(e)

                logging.info("Database pool stats: %s", pool_stats)

                if pool_stats.get("utilization", 0) > 80:
                    logging.warning(
                        "Database pool high utilization: %.1f%%",
                        pool_stats["utilization"],
                    )

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.warning("Connection pool monitoring error: %s", e)
                await asyncio.sleep(60)

    async def cleanup_expired_cache(self):
        """Deprecated: TTLCache handles eviction automatically."""
        pass

    async def query(
        self, query_str: str, params: tuple = (), retries: int = 3, conn=None
    ):
        if not isinstance(query_str, str) or not query_str.strip():
            raise ValueError("Query must be a non-empty string")

        if conn:
            try:
                result = await conn.fetch(query_str, *params)
                return [dict(record) for record in result]
            except Exception as e:
                logging.error(f"Error in provided connection query: {e}")
                raise e

        last_exception = None
        for attempt in range(retries + 1):
            try:
                if not self.pool or self.pool._closed:
                    logging.warning(
                        "Database pool not initialized or closed – attempting reconnect..."
                    )
                    await self.reconnect()
                    if not self.pool or self.pool._closed:
                        raise Exception("Database pool is closed")

                async with self.pool.acquire() as connection:
                    result = await asyncio.wait_for(
                        connection.fetch(query_str, *params), timeout=30.0
                    )
                    return [dict(record) for record in result]

            except asyncio.TimeoutError:
                last_exception = Exception(
                    f"Database query timeout: {query_str[:100]}..."
                )
                logging.warning(f"Database query timeout (attempt {attempt + 1})")

            except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError) as e:
                last_exception = e
                logging.warning(
                    f"Database connection issue (attempt {attempt + 1}): {e}"
                )
                if attempt < retries:
                    await asyncio.sleep(min(2**attempt, 10))
                    try:
                        await self.reconnect()
                    except Exception:
                        pass
                    continue

            except Exception as e:
                last_exception = e
                if "rate limit" in str(e).lower():
                    raise
                logging.error(f"Database query error (attempt {attempt + 1}): {e}")
                if attempt == retries:
                    break
                await asyncio.sleep(min(2**attempt, 10))

        raise last_exception or Exception("Database query failed")

    async def execute_many(
        self, query_str: str, params_list: List[tuple], retries: int = 3, conn=None
    ):
        if not isinstance(query_str, str) or not query_str.strip():
            raise ValueError("Query must be a non-empty string")

        if not params_list:
            return

        if conn:
            try:
                await conn.executemany(query_str, params_list)
                return
            except Exception as e:
                logging.error(f"Error in provided connection executemany: {e}")
                raise e

        last_exception = None
        for attempt in range(retries + 1):
            try:
                if not self.pool or self.pool._closed:
                    logging.warning(
                        "Database pool not initialized or closed – attempting reconnect..."
                    )
                    await self.reconnect()
                    if not self.pool or self.pool._closed:
                        raise Exception("Database pool is closed")

                async with self.pool.acquire() as connection:
                    await asyncio.wait_for(
                        connection.executemany(query_str, params_list), timeout=30.0
                    )
                    return

            except asyncio.TimeoutError:
                last_exception = Exception(
                    f"Database executemany timeout: {query_str[:100]}..."
                )
                logging.warning(f"Database executemany timeout (attempt {attempt + 1})")

            except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError) as e:
                last_exception = e
                logging.warning(
                    f"Database connection issue (attempt {attempt + 1}): {e}"
                )
                if attempt < retries:
                    await asyncio.sleep(min(2**attempt, 10))
                    try:
                        await self.reconnect()
                    except Exception:
                        pass
                    continue

            except Exception as e:
                last_exception = e
                if "rate limit" in str(e).lower():
                    raise
                logging.error(
                    f"Database executemany error (attempt {attempt + 1}): {e}"
                )
                if attempt == retries:
                    break
                await asyncio.sleep(min(2**attempt, 10))

        raise last_exception or Exception("Database executemany failed")


# Global instances
db_manager = DatabaseManager()
db_pool = None  # Backward compatibility

# Module-level functions delegating to db_manager


async def reconnect_database():
    return await db_manager.reconnect()


async def db_query(query: str, params: tuple = (), retries: int = 3, conn=None):
    return await db_manager.query(query, params, retries, conn)


async def db_execute_many(
    query: str, params_list: List[tuple], retries: int = 3, conn=None
):
    return await db_manager.execute_many(query, params_list, retries, conn)


async def check_database_health():
    if not db_manager.pool:
        return False
    if db_manager.pool._closed:
        return False
    try:
        async with db_manager.pool.acquire() as conn:
            await conn.execute("SELECT 1")
            return True
    except Exception:
        return False


def is_database_connected() -> bool:
    """Synchronous database connectivity check for web/status endpoints."""
    return bool(db_pool and not db_pool._closed)


async def ensure_database_connection():
    if not await check_database_health():
        try:
            return await db_manager.reconnect()
        except Exception:
            return False
    return True


# --- Business Logic (Refactored to use db_manager) ---


async def init_db():
    if not settings.DATABASE_URL:
        raise Exception("DATABASE_URL not set")

    await db_manager.create_pool()
    if not db_manager.pool:
        raise Exception("Critical: Failed to create database connection pool")

    # Apply Supabase optimizations
    try:
        async with db_manager.pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '60s'")
            await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
            await conn.execute("SET lock_timeout = '30s'")
    except Exception as e:
        logging.warning(f"Failed to apply DB optimizations: {e}")

    # Initialize Schema
    await _init_schema()

    # Start cache cleanup
    await db_manager.start_cleanup_task()


async def _init_schema():
    await db_query(
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0, is_deep_dive BOOLEAN DEFAULT FALSE)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)"
    )

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

    await db_query(
        "CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS openrouter_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS openrouter_key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))"
    )

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

    await setup_row_level_security()
    await _run_migrations()
    await _insert_initial_data()


async def _run_migrations():
    try:
        # Document Table Migration
        doc_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='user_documents'"
        )
        doc_column_names = {c["column_name"] for c in doc_columns}

        if "filename" not in doc_column_names and "file_name" in doc_column_names:
            await db_query(
                "ALTER TABLE user_documents RENAME COLUMN file_name TO filename;"
            )
        elif "filename" not in doc_column_names:
            await db_query("ALTER TABLE user_documents ADD COLUMN filename TEXT;")

        required_columns = {
            "content": "TEXT",
            "pages": "INTEGER",
            "file_size": "BIGINT",
            "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP",
        }
        for col, col_type in required_columns.items():
            if col not in doc_column_names:
                await db_query(
                    f"ALTER TABLE user_documents ADD COLUMN {col} {col_type};"
                )

        # Tavily Key Usage Migration
        tavily_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tavily_key_usage'"
        )
        if "request_count" in {c["column_name"] for c in tavily_columns}:
            await db_query(
                "ALTER TABLE tavily_key_usage RENAME COLUMN request_count TO credit_usage;"
            )

        # Users Table Migration
        users_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users'"
        )
        if "is_deep_dive" not in {c["column_name"] for c in users_columns}:
            await db_query(
                "ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;"
            )

        if "deep_dive_thread_id" not in {c["column_name"] for c in users_columns}:
            await db_query("ALTER TABLE users ADD COLUMN deep_dive_thread_id TEXT;")

    except asyncpg.PostgresError as e:
        logging.warning(f"Migration warning: {e}")


async def _insert_initial_data():
    await db_query(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING",
        (settings.ADMIN_ID,),
    )

    gemini_data = [
        (hashlib.sha256(key.encode()).hexdigest(), key)
        for key in settings.GEMINI_API_KEYS
    ]
    if gemini_data:
        await db_execute_many(
            "INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING",
            gemini_data,
        )

    tavily_data = [
        (hashlib.sha256(key.encode()).hexdigest(), key)
        for key in settings.TAVILY_API_KEYS
    ]
    if tavily_data:
        await db_execute_many(
            "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING",
            tavily_data,
        )

    openrouter_data = [
        (hashlib.sha256(key.encode()).hexdigest(), key)
        for key in settings.OPENROUTER_API_KEYS
    ]
    if openrouter_data:
        await db_execute_many(
            "INSERT INTO openrouter_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO NOTHING",
            openrouter_data,
        )

    await db_query("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
    await db_query(
        "CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv_id ON conversation_messages(conversation_id)"
    )
    await db_query(
        "CREATE INDEX IF NOT EXISTS idx_key_usage_model_date ON key_usage(model_name, usage_date)"
    )


# RLS Helper functions
VALID_TABLES = {
    "users",
    "chats",
    "roles",
    "user_roles",
    "conversations",
    "conversation_messages",
    "user_documents",
    "api_keys",
    "key_usage",
    "tavily_api_keys",
    "tavily_key_usage",
    "openrouter_api_keys",
    "openrouter_key_usage",
    "group_chats",
    "group_members",
    "group_messages",
    "metrics",
    "error_logs",
}


async def setup_row_level_security():
    """Настраивает Row Level Security для всех таблиц"""
    try:
        # Быстрая проверка, настроены ли уже политики (чтобы не гонять ALTER TABLE при каждом рестарте)
        existing = await db_query(
            "SELECT 1 FROM pg_policies WHERE tablename = 'users' AND policyname = 'users_policy'"
        )
        if existing:
            logging.info("RLS already configured, skipping setup.")
            return

        for table in VALID_TABLES:
            try:
                await db_query(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
                await create_rls_policies(table)
            except Exception as e:
                logging.warning(f"Failed to enable RLS for table {table}: {e}")
    except Exception as e:
        logging.error(f"Error setting up RLS: {e}")


async def create_rls_policies(table_name: str):
    """Создает политики безопасности для таблицы"""
    if table_name not in VALID_TABLES:
        logging.error(f"Invalid table name for RLS policy: {table_name}")
        return

    try:
        if table_name == "users":
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'users' AND policyname = 'users_policy'
                """)
                        CREATE POLICY users_policy ON users
                        FOR ALL USING (
                            user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR 
                            (select current_setting('app.is_admin', true)) = 'true'
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create users_policy: {e}")
                raise e

        elif table_name == "chats":
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'chats' AND policyname = 'chats_policy'
                """)
                        CREATE POLICY chats_policy ON chats
                        FOR ALL USING (
                            user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR 
                            (select current_setting('app.is_admin', true)) = 'true'
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create chats_policy: {e}")
                raise e

        elif table_name == "user_documents":
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'user_documents' AND policyname = 'user_documents_policy'
                """)
                        CREATE POLICY user_documents_policy ON user_documents
                        FOR ALL USING (
                            user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR 
                            (select current_setting('app.is_admin', true)) = 'true'
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create user_documents_policy: {e}")
                raise e

        elif table_name == "roles":
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
                    WHERE tablename = 'roles' AND policyname = 'roles_update_policy'
                """)
                if not existing_write:
                    await db_query("""
                        CREATE POLICY roles_insert_policy ON roles FOR INSERT WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');
                        CREATE POLICY roles_update_policy ON roles FOR UPDATE USING ((select current_setting('app.is_admin', true)) = 'true');
                        CREATE POLICY roles_delete_policy ON roles FOR DELETE USING ((select current_setting('app.is_admin', true)) = 'true');
                    """)
            except Exception as e:
                logging.error(f"Failed to create roles policies: {e}")
                raise e

        elif table_name == "user_roles":
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'user_roles' AND policyname = 'user_roles_policy'
                """)
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY user_roles_policy ON user_roles
                        FOR ALL USING (
                            user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR 
                            (select current_setting('app.is_admin', true)) = 'true'
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create user_roles policy: {e}")
                raise e

        elif table_name == "conversations":
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'conversations' AND policyname = 'conversations_policy'
                """)
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY conversations_policy ON conversations
                        FOR ALL USING (
                            user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR 
                            (select current_setting('app.is_admin', true)) = 'true'
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create conversations policy: {e}")
                raise e

        elif table_name == "conversation_messages":
            try:
                existing_policy = await db_query("""
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = 'conversation_messages' AND policyname = 'conversation_messages_policy'
                """)
                if not existing_policy:
                    await db_query("""
                        CREATE POLICY conversation_messages_policy ON conversation_messages
                        FOR ALL USING (
                            (select current_setting('app.is_admin', true)) = 'true'
                            OR EXISTS (
                                SELECT 1 FROM conversations c 
                                WHERE c.id = conversation_messages.conversation_id
                                  AND c.user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint
                            )
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create conversation_messages policy: {e}")
                raise e

        elif table_name in ["group_chats", "group_members", "group_messages"]:
            try:
                existing_policy = await db_query(
                    """
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = $1 AND policyname = $2
                """,
                    (table_name, f"{table_name}_policy"),
                )

                if not existing_policy:
                    await db_query(f"""
                        CREATE POLICY {table_name}_policy ON {table_name}
                        FOR ALL USING (
                            (select current_setting('app.is_admin', true)) = 'true' OR
                            EXISTS (
                                SELECT 1 FROM group_members gm 
                                WHERE gm.chat_id = {table_name}.chat_id 
                                AND gm.user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint
                            )
                        );
                    """)
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e

        elif table_name in [
            "api_keys",
            "key_usage",
            "tavily_api_keys",
            "tavily_key_usage",
            "openrouter_api_keys",
            "openrouter_key_usage",
            "metrics",
            "error_logs",
        ]:
            try:
                existing_policy = await db_query(
                    """
                    SELECT 1 FROM pg_policies 
                    WHERE tablename = $1 AND policyname = $2
                """,
                    (table_name, f"{table_name}_policy"),
                )

                if not existing_policy:
                    await db_query(f"""
                        CREATE POLICY {table_name}_policy ON {table_name}
                        FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
                    """)
            except Exception as e:
                logging.error(f"Failed to create {table_name}_policy: {e}")
                raise e

    except Exception as e:
        logging.error(f"Error creating RLS policies for {table_name}: {e}")


async def set_user_context(user_id: int, is_admin: bool = False, conn=None):
    try:
        await db_query(
            """
            SELECT 
                set_config('app.user_id', $1, false),
                set_config('app.is_admin', $2, false)
        """,
            (str(user_id), str(is_admin).lower()),
            conn=conn,
        )
    except Exception as e:
        logging.warning(f"Failed to set user context: {e}")


async def clear_user_context(conn=None):
    try:
        await db_query(
            """
            SELECT 
                set_config('app.user_id', '', false),
                set_config('app.is_admin', 'false', false)
        """,
            conn=conn,
        )
    except Exception as e:
        logging.warning(f"Failed to clear user context: {e}")


async def get_user_chat(user_id: int) -> ChatState:
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, is_admin(user_id), conn=conn)
        try:
            # Optimized: Combine users and chats query into one
            query = """
                SELECT
                    c.history, c.model, c.token_count, c.search_enabled, c.system_prompt,
                    u.is_deep_dive, u.deep_dive_thread_id
                FROM users u
                LEFT JOIN chats c ON u.user_id = c.user_id
                WHERE u.user_id = $1
            """
            result = await db_query(query, (user_id,), conn=conn)

            chat_state = ChatState(
                history=[],
                model=settings.DEFAULT_MODEL,
                token_count=0,
                search_enabled=False,
                system_prompt=None,
                is_deep_dive=False,
                deep_dive_thread_id=None,
            )

            if result:
                row = result[0]
                # Chat fields
                if row["history"]:
                    chat_state.history = json.loads(row["history"])
                else:
                    chat_state.history = []

                chat_state.model = row["model"] or settings.DEFAULT_MODEL
                chat_state.token_count = row["token_count"] or 0
                chat_state.search_enabled = (
                    bool(row["search_enabled"])
                    if row["search_enabled"] is not None
                    else False
                )
                chat_state.system_prompt = row["system_prompt"] or None

                # User fields
                chat_state.is_deep_dive = row["is_deep_dive"] or False
                chat_state.deep_dive_thread_id = row.get("deep_dive_thread_id")

            return chat_state
        finally:
            await clear_user_context(conn=conn)


async def update_user_chat(user_id: int, chat_state: ChatState):
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, is_admin(user_id), conn=conn)

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
            await db_query(
                chat_query,
                (
                    user_id,
                    history_json,
                    chat_state.model,
                    chat_state.token_count,
                    int(chat_state.search_enabled),
                    chat_state.system_prompt,
                ),
                conn=conn,
            )

            user_query = "UPDATE users SET is_deep_dive = $1, deep_dive_thread_id = $2 WHERE user_id = $3"
            await db_query(
                user_query,
                (chat_state.is_deep_dive, chat_state.deep_dive_thread_id, user_id),
                conn=conn,
            )
        finally:
            await clear_user_context(conn=conn)


async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    # Optimistic cache check (no DB lock needed)
    cached_key = None
    async with db_manager._cache_lock:
        if model_name in db_manager._active_keys_cache:
            cached_key = db_manager._active_keys_cache[model_name]

    # Optimization: Trust the cache if valid. Invalidation is handled by increment_gemini_key_usage.
    if cached_key:
        return cached_key

    # If we need to fetch, we need a connection context
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            # Fetch new key if cache missed or was invalid
            new_key = await _get_fresh_available_key(model_name, conn=conn)

            if new_key:
                async with db_manager._cache_lock:
                    db_manager._active_keys_cache[model_name] = new_key

            return new_key
        finally:
            await clear_user_context(conn=conn)


async def _is_key_available(key_hash: str, model_name: str, conn=None) -> bool:
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)

    if not daily_limit:
        return True

    query = """
        SELECT COALESCE(request_count, 0) as request_count
        FROM key_usage 
        WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3
    """

    result = await db_query(query, (key_hash, model_name, today_pacific), conn=conn)
    current_usage = result[0]["request_count"] if result else 0
    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT

    return current_usage < threshold


async def _get_fresh_available_key(
    model_name: str, conn=None
) -> Optional[Dict[str, Any]]:
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)

    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys LIMIT 1", conn=conn)
        return keys[0] if keys else None

    query = """
        SELECT ak.key_hash, ak.api_key, COALESCE(ku.request_count, 0) as request_count
        FROM api_keys ak
        LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash 
            AND ku.model_name = $1 AND ku.usage_date = $2
        ORDER BY COALESCE(ku.request_count, 0) ASC
    """

    results = await db_query(query, (model_name, today_pacific), conn=conn)

    if not results:
        return None

    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT

    for row in results:
        if row["request_count"] < threshold:
            return {"key_hash": row["key_hash"], "api_key": row["api_key"]}

    return None


async def invalidate_key_cache(model_name: str = None):
    async with db_manager._cache_lock:
        if model_name:
            if model_name in db_manager._active_keys_cache:
                del db_manager._active_keys_cache[model_name]
        else:
            db_manager._active_keys_cache.clear()


async def get_current_active_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    daily_limit = settings.DAILY_LIMITS.get(model_name)

    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys LIMIT 1")
        return keys[0] if keys else None

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
        return {"key_hash": results[0]["key_hash"], "api_key": results[0]["api_key"]}

    return None


async def increment_gemini_key_usage(key_hash: str, model_name: str):
    today_pacific: date = datetime.now(get_pacific_tz()).date()

    # Optimization: Use RETURNING to avoid extra SELECT
    query = """
        INSERT INTO key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, 1)
        ON CONFLICT (key_hash, model_name, usage_date)
        DO UPDATE SET request_count = key_usage.request_count + 1
        RETURNING request_count;
    """
    result = await db_query(query, (key_hash, model_name, today_pacific))
    current_usage = result[0]["request_count"] if result else 0

    daily_limit = settings.DAILY_LIMITS.get(model_name)
    if daily_limit:
        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT

        if current_usage >= threshold:
            await invalidate_key_cache(model_name)
        else:
            async with db_manager._cache_lock:
                if model_name in db_manager._cache_last_updated:
                    db_manager._cache_last_updated[model_name] = time.time()


async def get_available_tavily_key():
    current_month = datetime.now(UTC_TZ).strftime("%Y-%m")
    query = """
        SELECT tak.key_hash, tak.api_key, COALESCE(tku.credit_usage, 0) as credit_usage
        FROM tavily_api_keys tak
        LEFT JOIN tavily_key_usage tku ON tak.key_hash = tku.key_hash 
            AND tku.usage_month = $1
        ORDER BY COALESCE(tku.credit_usage, 0) ASC
    """

    results = await db_query(query, (current_month,))
    threshold = (
        settings.TAVILY_MONTHLY_CREDIT_LIMIT * settings.TAVILY_LIMIT_THRESHOLD_PERCENT
    )

    for row in results:
        if row["credit_usage"] < threshold:
            return {"key_hash": row["key_hash"], "api_key": row["api_key"]}

    return None


async def increment_tavily_key_usage(key_hash: str, cost: int):
    current_month = datetime.now(UTC_TZ).strftime("%Y-%m")
    query = """
    INSERT INTO tavily_key_usage (key_hash, usage_month, credit_usage) VALUES ($1, $2, $3)
    ON CONFLICT (key_hash, usage_month)
    DO UPDATE SET credit_usage = tavily_key_usage.credit_usage + $4;
    """
    await db_query(query, (key_hash, current_month, cost, cost))


async def get_available_openrouter_key(model_name: str) -> Optional[Dict[str, Any]]:
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            today_pacific: date = datetime.now(get_pacific_tz()).date()
            query = """
                SELECT oak.key_hash, oak.api_key, COALESCE(oku.request_count, 0) as request_count
                FROM openrouter_api_keys oak
                LEFT JOIN openrouter_key_usage oku ON oak.key_hash = oku.key_hash
                    AND oku.model_name = $1 AND oku.usage_date = $2
                ORDER BY COALESCE(oku.request_count, 0) ASC
                LIMIT 1
            """
            results = await db_query(query, (model_name, today_pacific), conn=conn)

            if results:
                return {
                    "key_hash": results[0]["key_hash"],
                    "api_key": results[0]["api_key"],
                }

            return None
        finally:
            await clear_user_context(conn=conn)


async def increment_openrouter_key_usage(key_hash: str, model_name: str):
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    query = """
        INSERT INTO openrouter_key_usage (key_hash, model_name, usage_date, request_count) VALUES ($1, $2, $3, 1)
        ON CONFLICT (key_hash, model_name, usage_date)
        DO UPDATE SET request_count = openrouter_key_usage.request_count + 1;
    """
    await db_query(query, (key_hash, model_name, today_pacific))


async def optimize_database_connections():
    if not db_manager.pool:
        return False
    try:
        async with db_manager.pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '60s'")
            await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
            await conn.execute("SET lock_timeout = '30s'")
        return True
    except Exception:
        return False


async def get_supabase_metrics() -> Dict[str, Any]:
    if not db_manager.pool:
        return {"status": "disconnected", "pool_size": 0, "active_connections": 0}
    try:
        pool = db_manager.pool
        pool_stats = {
            "status": "connected" if not pool._closed else "closed",
            "pool_size": pool.get_size(),
            "free_size": pool.get_free_size(),
            "active_connections": pool.get_size() - pool.get_free_size(),
        }
        start_time = time.time()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            response_time = time.time() - start_time
            pool_stats.update(
                {
                    "response_time_ms": round(response_time * 1000, 2),
                    "connection_health": "healthy" if response_time < 0.1 else "slow",
                }
            )
        return pool_stats
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "pool_size": 0,
            "active_connections": 0,
        }


async def get_gemini_key_usage_stats(model_name: str = None) -> List[Dict[str, Any]]:
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    if model_name:
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
        results = await db_query(
            query,
            (
                model_name,
                settings.DAILY_LIMITS.get(model_name),
                settings.LIMIT_THRESHOLD_PERCENT,
                today_pacific,
            ),
        )
    else:
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
        results = await db_query(
            query, (settings.LIMIT_THRESHOLD_PERCENT, today_pacific)
        )
    return results


async def get_active_key_info(model_name: str) -> Optional[Dict[str, Any]]:
    # 1. Get from cache without I/O
    cached_key = None
    cached_at = time.time()  # approximate since we don't track explicitly anymore
    async with db_manager._cache_lock:
        if model_name in db_manager._active_keys_cache:
            cached_key = db_manager._active_keys_cache[model_name]

    if not cached_key:
        return None

    # 2. Verify availability outside lock with proper context
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            is_available = await _is_key_available(
                cached_key["key_hash"], model_name, conn=conn
            )
            return {
                "key_hash": cached_key["key_hash"],
                "api_key_preview": cached_key["api_key"][:10] + "...",
                "is_available": is_available,
                "cached_at": cached_at,
            }
        finally:
            await clear_user_context(conn=conn)


async def force_update_tavily_keys():
    try:
        from app.config import get_settings

        settings = get_settings()
        if not settings or not settings.TAVILY_API_KEYS:
            return False
        await db_query("DELETE FROM tavily_api_keys")
        keys_data = []
        for key in settings.TAVILY_API_KEYS:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            keys_data.append((key_hash, key))

        if keys_data:
            await db_execute_many(
                "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2)",
                keys_data,
            )
        await db_query("DELETE FROM tavily_key_usage")
        async with db_manager._cache_lock:
            db_manager._active_keys_cache.clear()
        return True
    except Exception:
        return False


async def invalidate_user_auth_cache(user_id: int):
    async with db_manager._cache_lock:
        if user_id in db_manager._user_auth_cache:
            del db_manager._user_auth_cache[user_id]


def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    # Check cache
    async with db_manager._cache_lock:
        if user_id in db_manager._user_auth_cache:
            return db_manager._user_auth_cache[user_id]

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, False, conn=conn)
        try:
            result = await db_query(
                "SELECT is_authorized FROM users WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            is_auth = result and result[0]["is_authorized"] == 1

            # Update cache
            async with db_manager._cache_lock:
                db_manager._user_auth_cache[user_id] = is_auth

            return is_auth
        finally:
            await clear_user_context(conn=conn)


async def get_role_data(role_key: str, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Получает данные роли (название, промпт) по ключу.
    Поддерживает системные роли (из prompts.py) и пользовательские (из БД).
    """
    from app import prompts

    if not role_key:
        return None

    if role_key.startswith("user_role:"):
        try:
            # Извлекаем ID из ключа "user_role:ID"
            role_id = int(role_key.split(":")[1])
            res = await db_query(
                "SELECT id, title, prompt FROM user_roles WHERE id = $1 AND user_id = $2",
                (role_id, user_id),
            )
            if res:
                return {
                    "id": res[0]["id"],
                    "title": res[0]["title"],
                    "prompt": res[0]["prompt"],
                    "is_custom": True,
                    "key": role_key,
                }
        except (ValueError, IndexError, Exception):
            pass
    elif role_key in prompts.DEFAULT_ROLES:
        meta = prompts.DEFAULT_ROLES[role_key]
        return {
            "id": None,
            "title": meta.get("title", role_key),
            "prompt": meta.get("prompt", ""),
            "is_custom": False,
            "key": role_key,
        }

    return None


async def save_conversation(
    user_id: int, title: str, role_type: str = None, role_id: int = None
) -> int:
    try:
        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return None
        result = await db_query(
            """INSERT INTO conversations (user_id, title, role_type, role_id, summary, token_budget, created_at) 
               VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP) RETURNING id""",
            (user_id, title, role_type, role_id, None, chat_state.token_count),
        )
        conv_id = result[0]["id"] if result else None
        if conv_id and chat_state.history:
            try:
                if isinstance(chat_state.history, list):
                    history_data = {"messages": chat_state.history}
                else:
                    history_data = json.loads(chat_state.history)

                roles_to_insert = []
                contents_to_insert = []

                for msg in history_data.get("messages", []):
                    if isinstance(msg, dict):
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(str(part) for part in content)
                        text_lower = (content or "").strip()
                        if role not in ("user", "assistant"):
                            continue
                        if text_lower.startswith("/"):
                            continue
                        if any(
                            prefix in text_lower
                            for prefix in (
                                "🖼️ обрабатываю изображение",
                                "🤔 думаю",
                                "📄 обрабатываю документ",
                                "✅ новый чат создан",
                                "опишите, какую роль хотите создать",
                                "не удалось сгенерировать роль",
                                "сервер перегружен",
                            )
                        ):
                            continue
                    else:
                        role = "user"
                        content = str(msg)
                    roles_to_insert.append(role)
                    contents_to_insert.append(content)

                if roles_to_insert:
                    await db_query(
                        """INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                           SELECT $1, u.role, u.content, CURRENT_TIMESTAMP
                           FROM unnest($2::text[], $3::text[]) AS u(role, content)""",
                        (conv_id, roles_to_insert, contents_to_insert),
                    )
            except Exception as e:
                logging.error(f"Error saving conversation messages: {e}")
        return conv_id
    except Exception as e:
        logging.error(f"Error in save_conversation: {e}")
        return None


async def get_user_conversations(
    user_id: int, limit: int = 10, offset: int = 0
) -> list:
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
            (user_id, limit, offset),
        )
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "role_type": row["role_type"],
                "role_id": row["role_id"],
                "summary": row["summary"],
                "token_budget": row["token_budget"],
                "created_at": row["created_at"],
                "role_title": row["role_title"] or row["user_role_title"],
            }
            for row in result
        ]
    except Exception:
        return []


async def get_conversation_messages(conversation_id: int, user_id: int) -> list:
    try:
        query = """
            SELECT cm.role, cm.content, cm.created_at
            FROM conversations c
            LEFT JOIN conversation_messages cm ON c.id = cm.conversation_id
            WHERE c.id = $1 AND c.user_id = $2
            ORDER BY cm.created_at ASC
        """
        result = await db_query(query, (conversation_id, user_id))

        if not result:
            return None

        # If the conversation exists but has no messages, the left join returns one row with NULLs
        if result[0]["role"] is None:
            return []

        return [
            {
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in result
        ]
    except Exception:
        return None


async def switch_to_conversation(user_id: int, conversation_id: int) -> bool:
    try:
        conv_data = await db_query(
            "SELECT role_type, role_id, summary FROM conversations WHERE id = $1 AND user_id = $2",
            (conversation_id, user_id),
        )
        if not conv_data:
            return False
        role_type, role_id, summary = (
            conv_data[0]["role_type"],
            conv_data[0]["role_id"],
            conv_data[0]["summary"],
        )
        messages = await get_conversation_messages(conversation_id, user_id)
        if messages is None:
            return False

        history_data = {
            "messages": messages,
            "conversation_id": conversation_id,
            "summary": summary,
        }
        history_json = json.dumps(history_data, ensure_ascii=False)

        await db_query(
            "UPDATE chats SET history = $1, token_count = 0 WHERE user_id = $2",
            (history_json, user_id),
        )

        if role_type and role_id:
            role_data = None
            if role_type == "role":
                role_data = await db_query(
                    "SELECT prompt FROM roles WHERE id = $1", (role_id,)
                )
            elif role_type == "user_role":
                role_data = await db_query(
                    "SELECT prompt FROM user_roles WHERE id = $1", (role_id,)
                )

            if role_data:
                await db_query(
                    "UPDATE chats SET system_prompt = $1 WHERE user_id = $2",
                    (role_data[0]["prompt"], user_id),
                )
        return True
    except Exception:
        return False


async def rename_conversation(
    user_id: int, conversation_id: int, new_title: str
) -> bool:
    try:
        result = await db_query(
            "UPDATE conversations SET title = $1 WHERE id = $2 AND user_id = $3",
            (new_title, conversation_id, user_id),
        )
        return result is not None
    except Exception:
        return False


async def delete_conversation(user_id: int, conversation_id: int) -> bool:
    try:
        conv_check = await db_query(
            "SELECT id FROM conversations WHERE id = $1 AND user_id = $2",
            (conversation_id, user_id),
        )
        if not conv_check:
            return False
        await db_query(
            "DELETE FROM conversation_messages WHERE conversation_id = $1",
            (conversation_id,),
        )
        await db_query(
            "DELETE FROM conversations WHERE id = $1 AND user_id = $2",
            (conversation_id, user_id),
        )
        return True
    except Exception:
        return False


async def get_conversation_count(user_id: int) -> int:
    try:
        result = await db_query(
            "SELECT COUNT(*) FROM conversations WHERE user_id = $1", (user_id,)
        )
        return result[0]["count"] if result else 0
    except Exception:
        return 0
