import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.config import settings
from app.errors import (
    ConfigurationError,
    DatabaseConnectionError,
    DatabasePoolError,
    DatabaseRateLimitError,
)


@dataclass
class ChatState:
    history: list[dict[str, Any]]
    model: str
    token_count: int
    search_enabled: bool
    system_prompt: str | None
    is_deep_dive: bool = False
    deep_dive_thread_id: str | None = None
    _original_length: int = 0


class DatabaseManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
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
            async def _init_connection(conn):
                """Apply session-level settings to every new connection."""
                await conn.execute("SET statement_timeout = '60s'")
                await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
                await conn.execute("SET lock_timeout = '30s'")

            self.pool = await asyncpg.create_pool(
                dsn=settings.DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=30,
                init=_init_connection,
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
                raise DatabaseRateLimitError(f"Database rate limit exceeded: {e}") from e
            elif "connection" in str(e).lower() or "timeout" in str(e).lower():
                logging.warning(
                    "Database connection issue: %s. This might be temporary.", e
                )
                raise DatabaseConnectionError(f"Database connection failed: {e}") from e
            else:
                logging.error("Unexpected database error: %s", e)
                raise DatabasePoolError(f"Database initialization failed: {e}") from e

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
        from app.utils.background_tasks import start_background_task
        return start_background_task(task_ref, coro_factory, task_name)

    async def _cancel_background_task(self, attr_name: str):
        """Отменяет и ожидает завершение фоновой задачи по имени атрибута."""
        from app.utils.background_tasks import cancel_background_task
        await cancel_background_task(self, attr_name)

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

                logging.debug("Database pool stats: %s", pool_stats)

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

    async def _execute_with_retry(self, operation_name: str, operation, query_str: str, retries: int = 3):
        """Shared retry/reconnect/backoff loop for all pooled database operations.

        Args:
            operation_name: Human-readable name for logging (e.g. "query", "executemany").
            operation: An async callable(connection) -> result that performs the actual DB work.
            query_str: The SQL query (used for timeout error messages).
            retries: Number of retry attempts.
        """
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
                    return await asyncio.wait_for(operation(connection), timeout=30.0)

            except TimeoutError:
                last_exception = Exception(
                    f"Database {operation_name} timeout: {query_str[:100]}..."
                )
                logging.warning("Database %s timeout (attempt %s)", operation_name, attempt + 1)

            except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError) as e:
                last_exception = e
                logging.warning(
                    "Database connection issue (attempt %s): %s", attempt + 1, e
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
                logging.error("Database %s error (attempt %s): %s", operation_name, attempt + 1, e)
                if attempt == retries:
                    break
                await asyncio.sleep(min(2**attempt, 10))

        raise last_exception or Exception(f"Database {operation_name} failed")

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
                logging.error("Error in provided connection query: %s", e, exc_info=True)
                raise e

        async def _do_fetch(connection):
            result = await connection.fetch(query_str, *params)
            return [dict(record) for record in result]

        return await self._execute_with_retry("query", _do_fetch, query_str, retries)

    async def execute_many(
        self, query_str: str, params_list: list[tuple], retries: int = 3, conn=None
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
                logging.error("Error in provided connection executemany: %s", e, exc_info=True)
                raise e

        async def _do_executemany(connection):
            await connection.executemany(query_str, params_list)

        await self._execute_with_retry("executemany", _do_executemany, query_str, retries)


# Global instances
db_manager = DatabaseManager()

# Module-level functions delegating to db_manager


async def reconnect_database():
    return await db_manager.reconnect()


async def db_query(query: str, params: tuple = (), retries: int = 3, conn=None):
    return await db_manager.query(query, params, retries, conn)


async def db_execute_many(
    query: str, params_list: list[tuple], retries: int = 3, conn=None
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

    # NOTE: Session-level settings (statement_timeout, lock_timeout, etc.)
    # are applied via _init_connection() callback on every pool connection.
    # No need to apply them again here.

    # Initialize Schema
    await _init_schema()


async def _init_schema():
    """Create tables, setup RLS, run migrations, and seed initial data."""
    from app.db.migrations import run_migrations
    from app.db.schema import create_tables
    from app.db.seed import insert_initial_data

    await create_tables(db_query)
    await setup_row_level_security()  # uses the wrapper defined below
    await run_migrations(db_query, db_manager)
    await insert_initial_data(db_query, db_execute_many, settings)


# --- RLS re-exports (backward compatibility) ---
# Import RLS config/helpers so existing `from app.database import X` keeps working.
from app.db.rls import (  # noqa: F401, E402
    RLS_CONFIG,
    VALID_TABLES,
)
from app.db.rls import (
    create_rls_policies as _create_rls_policies,
)
from app.db.rls import (
    setup_row_level_security as _setup_rls,
)


async def setup_row_level_security():
    """Backward-compatible wrapper."""
    await _setup_rls(db_query)


async def create_rls_policies(table_name: str):
    """Backward-compatible wrapper."""
    await _create_rls_policies(table_name, db_query)


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
        logging.error("Failed to set user context for user %s: %s", user_id, e, exc_info=True)
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
# All business logic is in the repos/ layer:
#   app.repos.users         - auth, user state, feedback
#   app.repos.chats         - chat state management
#   app.repos.keys          - API key management
#   app.repos.conversations - saved conversations
#   app.repos.roles         - custom user roles CRUD
#   app.repos.user_stats    - per-user metrics queries
#   app.repos.metrics_repo  - metrics queries
#   app.repos.analytics     - user analytics
# =============================================================================
