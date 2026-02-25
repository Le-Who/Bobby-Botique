import logging
import re
import json
import hashlib
import asyncio
from datetime import datetime, date
from app.config import UTC_TZ, settings
from app.utils.time import get_pacific_tz
from app.errors import (
    ConfigurationError,
    DatabaseConnectionError,
    DatabaseRateLimitError,
    DatabasePoolError,
)
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
    _original_length: int = 0


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
            cls._instance._model_config_cache = TTLCache(maxsize=50, ttl=3600)
            cls._instance._active_chats_cache = TTLCache(maxsize=1000, ttl=900)

            cls._instance._cache_lock = asyncio.Lock()
            cls._instance._monitor_task = None
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
                statement_cache_size=0,  # Required for PgBouncer transaction mode
                server_settings={
                    "application_name": "gemaibotv2",
                    "tcp_keepalives_idle": "30",
                    "tcp_keepalives_interval": "10",
                    "tcp_keepalives_count": "5",
                    "jit": "off",
                },
            )

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
                raise DatabaseRateLimitError(f"Database rate limit exceeded: {e}")
            elif "connection" in str(e).lower() or "timeout" in str(e).lower():
                logging.warning(
                    "Database connection issue: %s. This might be temporary.", e
                )
                raise DatabaseConnectionError(f"Database connection failed: {e}")
            else:
                logging.error("Unexpected database error: %s", e)
                raise DatabasePoolError(f"Database initialization failed: {e}")

    async def close(self):
        # Cancel background tasks
        await self._cancel_background_task("_monitor_task")

        if self.pool:
            await self.pool.close()
            self.pool = None
            logging.info("Database pool closed")

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

    async def query(
        self, query_str: str, params: tuple = (), retries: int = 3, conn=None
    ):
        if not isinstance(query_str, str) or not query_str.strip():
            raise ValueError("Query must be a non-empty string")

        if conn:
            try:
                result = await conn.fetch(query_str, *params)
                return [dict(record) for record in result]
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error("Error in provided connection query: %s", e)
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
                logging.warning("Database query timeout (attempt %s)", attempt + 1)

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

            except asyncpg.PostgresError as e:
                last_exception = e
                if "rate limit" in str(e).lower():
                    raise
                logging.error("Database query error (attempt %s): %s", attempt + 1, e)
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
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error("Error in provided connection executemany: %s", e)
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
                logging.warning("Database executemany timeout (attempt %s)", attempt + 1)

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

            except asyncpg.PostgresError as e:
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
    return db_manager.is_connected


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
        raise ConfigurationError("DATABASE_URL not set")

    await db_manager.create_pool()
    if not db_manager.pool:
        raise DatabasePoolError("Critical: Failed to create database connection pool")

    # Apply Supabase optimizations
    try:
        async with db_manager.pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '60s'")
            await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
            await conn.execute("SET lock_timeout = '30s'")
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to apply DB optimizations: %s", e)

    # Initialize Schema
    await _init_schema()


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
    await db_query("""
        CREATE TABLE IF NOT EXISTS metrics (
            id SERIAL PRIMARY KEY,
            metric_date DATE NOT NULL,
            request_count INTEGER DEFAULT 0,
            total_response_time REAL DEFAULT 0.0,
            error_count INTEGER DEFAULT 0,
            search_queries INTEGER DEFAULT 0,
            cache_hits INTEGER DEFAULT 0,
            cache_misses INTEGER DEFAULT 0,
            api_calls JSONB DEFAULT '{}',
            model_usage JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(metric_date)
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id SERIAL PRIMARY KEY,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            request_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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

    await db_query("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id BIGINT PRIMARY KEY,
            document_mode BOOLEAN DEFAULT FALSE,
            selected_document_id INTEGER,
            awaiting_custom_role_input BOOLEAN DEFAULT FALSE,
            generated_role JSONB,
            last_custom_role_prompt TEXT,
            generating_custom_role BOOLEAN DEFAULT FALSE,
            last_sent_message_text TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db_query("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            message_id BIGINT,
            rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await setup_row_level_security()
    await _run_migrations()
    await _insert_initial_data()


async def _run_migrations():
    """Run numbered SQL migration files from scripts/migrations/ with version tracking.

    Creates a `schema_migrations` table to track which migrations have been applied.
    Files are expected to be named NNN_description.sql (e.g. 001_create_metrics_tables.sql).
    Each file is executed inside a transaction; on error the migration is rolled back.
    """
    import pathlib

    # 1. Create version tracking table if it doesn't exist
    await db_query("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Get already-applied versions
    applied = await db_query("SELECT version FROM schema_migrations ORDER BY version")
    applied_versions = {row["version"] for row in applied}

    # 3. Find migration files
    migrations_dir = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "migrations"
    if not migrations_dir.exists():
        logging.info("No migrations directory found at %s — skipping file migrations", migrations_dir)
        # Fall through to legacy inline migrations below
    else:
        sql_files = sorted(migrations_dir.glob("*.sql"))

        for sql_file in sql_files:
            # Extract version: "001" from "001_create_metrics_tables.sql"
            version = sql_file.stem.split("_", 1)[0]

            if version in applied_versions:
                continue  # Already applied

            logging.info("Applying migration %s (%s)...", version, sql_file.name)

            try:
                sql_content = sql_file.read_text(encoding="utf-8")

                # Execute inside a transaction
                async with db_manager.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(sql_content)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)",
                            version, sql_file.name,
                        )

                logging.info("Migration %s applied successfully.", version)
                applied_versions.add(version)

            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error("Migration %s FAILED: %s", version, e)
                # Don't halt startup — log and continue
                break  # Stop at first failure to preserve order

    # 4. Legacy inline migrations (for environments without SQL files)
    #    These are idempotent column-add checks that run on every startup.
    try:
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

        users_columns = await db_query(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users'"
        )
        user_col_names = {c["column_name"] for c in users_columns}

        if "is_deep_dive" not in user_col_names:
            await db_query(
                "ALTER TABLE users ADD COLUMN is_deep_dive BOOLEAN DEFAULT FALSE;"
            )

        if "deep_dive_thread_id" not in user_col_names:
            await db_query("ALTER TABLE users ADD COLUMN deep_dive_thread_id TEXT;")

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Legacy migration warning: %s", e)


async def _insert_initial_data():
    await db_query(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING",
        (settings.ADMIN_ID,),
    )

    from app.crypto import encrypt_api_key

    gemini_data = [
        (hashlib.sha256(key.encode()).hexdigest(), encrypt_api_key(key))
        for key in settings.GEMINI_API_KEYS
    ]
    if gemini_data:
        await db_execute_many(
            "INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
            gemini_data,
        )

    tavily_data = [
        (hashlib.sha256(key.encode()).hexdigest(), encrypt_api_key(key))
        for key in settings.TAVILY_API_KEYS
    ]
    if tavily_data:
        await db_execute_many(
            "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
            tavily_data,
        )

    openrouter_data = [
        (hashlib.sha256(key.encode()).hexdigest(), encrypt_api_key(key))
        for key in settings.OPENROUTER_API_KEYS
    ]
    if openrouter_data:
        await db_execute_many(
            "INSERT INTO openrouter_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
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
RLS_POLICY_USER = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR
    (select current_setting('app.is_admin', true)) = 'true'
);
"""

RLS_POLICY_ADMIN = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
"""

RLS_POLICY_GROUP = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm
        WHERE gm.chat_id = {table_name}.chat_id
        AND gm.user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint
    )
);
"""

RLS_POLICY_CONVERSATION_MESSAGES = """
CREATE POLICY {policy_name} ON {table_name}
FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true'
    OR EXISTS (
        SELECT 1 FROM conversations c
        WHERE c.id = {table_name}.conversation_id
        AND c.user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint
    )
);
"""

RLS_CONFIG = {
    "users": [{"name": "users_policy", "template": RLS_POLICY_USER}],
    "chats": [{"name": "chats_policy", "template": RLS_POLICY_USER}],
    "user_documents": [{"name": "user_documents_policy", "template": RLS_POLICY_USER}],
    "user_roles": [{"name": "user_roles_policy", "template": RLS_POLICY_USER}],
    "user_state": [{"name": "user_state_policy", "template": RLS_POLICY_USER}],
    "feedback": [{"name": "feedback_policy", "template": RLS_POLICY_USER}],
    "conversations": [{"name": "conversations_policy", "template": RLS_POLICY_USER}],
    "roles": [
        {
            "name": "roles_read_policy",
            "sql": "CREATE POLICY roles_read_policy ON roles FOR SELECT USING (true);",
        },
        {
            "name": "roles_insert_policy",
            "sql": "CREATE POLICY roles_insert_policy ON roles FOR INSERT WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');",
        },
        {
            "name": "roles_update_policy",
            "sql": "CREATE POLICY roles_update_policy ON roles FOR UPDATE USING ((select current_setting('app.is_admin', true)) = 'true');",
        },
        {
            "name": "roles_delete_policy",
            "sql": "CREATE POLICY roles_delete_policy ON roles FOR DELETE USING ((select current_setting('app.is_admin', true)) = 'true');",
        },
    ],
    "conversation_messages": [
        {
            "name": "conversation_messages_policy",
            "template": RLS_POLICY_CONVERSATION_MESSAGES,
        }
    ],
    "group_chats": [{"name": "group_chats_policy", "template": RLS_POLICY_GROUP}],
    "group_members": [{"name": "group_members_policy", "template": RLS_POLICY_GROUP}],
    "group_messages": [{"name": "group_messages_policy", "template": RLS_POLICY_GROUP}],
    "api_keys": [{"name": "api_keys_policy", "template": RLS_POLICY_ADMIN}],
    "key_usage": [{"name": "key_usage_policy", "template": RLS_POLICY_ADMIN}],
    "tavily_api_keys": [
        {"name": "tavily_api_keys_policy", "template": RLS_POLICY_ADMIN}
    ],
    "tavily_key_usage": [
        {"name": "tavily_key_usage_policy", "template": RLS_POLICY_ADMIN}
    ],
    "openrouter_api_keys": [
        {"name": "openrouter_api_keys_policy", "template": RLS_POLICY_ADMIN}
    ],
    "openrouter_key_usage": [
        {"name": "openrouter_key_usage_policy", "template": RLS_POLICY_ADMIN}
    ],
    "metrics": [{"name": "metrics_policy", "template": RLS_POLICY_ADMIN}],
    "error_logs": [{"name": "error_logs_policy", "template": RLS_POLICY_ADMIN}],
    "model_configuration": [
        {"name": "model_configuration_policy", "template": RLS_POLICY_ADMIN}
    ],
}

VALID_TABLES = set(RLS_CONFIG.keys())

# Regex for safely validating SQL identifiers (table names) before interpolation
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


async def setup_row_level_security():
    """Настраивает Row Level Security для всех таблиц"""
    try:
        # Быстрая проверка, настроены ли уже политики (чтобы не гонять ALTER TABLE on каждом рестарте)
        existing = await db_query(
            "SELECT 1 FROM pg_policies WHERE tablename = 'users' AND policyname = 'users_policy'"
        )
        if existing:
            logging.info("RLS already configured, skipping setup.")
            return

        for table in VALID_TABLES:
            if not _SAFE_IDENTIFIER_RE.match(table):
                logging.error("Refusing to use unsafe table name in SQL: %s", table)
                continue
            try:
                await db_query(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
                await create_rls_policies(table)
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.warning("Failed to enable RLS for table %s: %s", table, e)
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Error setting up RLS: %s", e)


async def create_rls_policies(table_name: str):
    """Создает политики безопасности для таблицы"""
    if table_name not in VALID_TABLES:
        logging.error("Invalid table name for RLS policy: %s", table_name)
        return

    try:
        policies = RLS_CONFIG.get(table_name)
        if not policies:
            logging.warning("No RLS configuration found for table: %s", table_name)
            return

        for policy_cfg in policies:
            policy_name = policy_cfg["name"]

            try:
                # Check if policy exists
                existing_policy = await db_query(
                    "SELECT 1 FROM pg_policies WHERE tablename = $1 AND policyname = $2",
                    (table_name, policy_name),
                )

                if not existing_policy:
                    # Construct SQL
                    if "sql" in policy_cfg:
                        sql = policy_cfg["sql"]
                    elif "template" in policy_cfg:
                        sql = policy_cfg["template"].format(
                            policy_name=policy_name, table_name=table_name
                        )
                    else:
                        logging.error(
                            f"Missing SQL or template for policy {policy_name}"
                        )
                        continue

                    await db_query(sql)

            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error(
                    f"Failed to create policy {policy_name} for table {table_name}: {e}"
                )
                raise e

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Error creating RLS policies for %s: %s", table_name, e)


async def set_user_context(user_id: int, is_admin: bool = False, conn=None):
    try:
        await db_query(
            """
            SELECT 
                set_config('app.user_id', $1, true),
                set_config('app.is_admin', $2, true)
        """,
            (str(user_id), str(is_admin).lower()),
            conn=conn,
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Failed to set user context for user %s: %s", user_id, e)
        raise  # Propagate — callers must not run queries with stale RLS context


async def clear_user_context(conn=None):
    try:
        await db_query(
            """
            SELECT 
                set_config('app.user_id', '', true),
                set_config('app.is_admin', 'false', true)
        """,
            conn=conn,
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to clear user context: %s", e)



# =============================================================================
# All business logic has been moved to the repos/ layer:
#   app.repos.users         - auth, user state, feedback
#   app.repos.chats         - chat state management
#   app.repos.keys          - API key management
#   app.repos.conversations - saved conversations
#   app.repos.metrics_repo  - metrics queries
#   app.repos.analytics     - user analytics
#
# The re-exports below keep rom app.database import X working.
# =============================================================================

# =============================================================================
# RE-EXPORTS FROM REPOSITORY LAYER (lazy, to avoid circular imports)
#
# These functions are canonical in app/repos/. They are re-exported here so
# that existing `from app.database import X` and `db.X()` calls keep working.
# New code should import directly from the repos module.
# =============================================================================

_REPO_EXPORTS = {
    # app.repos.users
    "is_admin": "app.repos.users",
    "is_authorized": "app.repos.users",
    "invalidate_user_auth_cache": "app.repos.users",
    "load_user_state": "app.repos.users",
    "save_user_state": "app.repos.users",
    "save_feedback": "app.repos.users",
    # app.repos.conversations
    "get_role_data": "app.repos.conversations",
    "save_conversation": "app.repos.conversations",
    "get_user_conversations": "app.repos.conversations",
    "get_conversation_messages": "app.repos.conversations",
    "switch_to_conversation": "app.repos.conversations",
    "rename_conversation": "app.repos.conversations",
    "delete_conversation": "app.repos.conversations",
    "get_conversation_count": "app.repos.conversations",
    # app.repos.chats
    "get_user_chat": "app.repos.chats",
    "update_user_chat": "app.repos.chats",
    # app.repos.keys
    "get_model_daily_limit": "app.repos.keys",
    "get_available_gemini_key": "app.repos.keys",
    "invalidate_key_cache": "app.repos.keys",
    "get_current_active_gemini_key": "app.repos.keys",
    "increment_gemini_key_usage": "app.repos.keys",
    "get_available_tavily_key": "app.repos.keys",
    "increment_tavily_key_usage": "app.repos.keys",
    "get_available_openrouter_key": "app.repos.keys",
    "increment_openrouter_key_usage": "app.repos.keys",
    "force_update_tavily_keys": "app.repos.keys",
    # app.repos.metrics_repo
    "get_gemini_key_usage_stats": "app.repos.metrics_repo",
    "get_active_key_info": "app.repos.metrics_repo",
}


def __getattr__(name: str):
    if name in _REPO_EXPORTS:
        import importlib
        mod = importlib.import_module(_REPO_EXPORTS[name])
        attr = getattr(mod, name)
        # Cache for subsequent lookups
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

