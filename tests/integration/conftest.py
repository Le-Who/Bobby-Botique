import pytest

"""Integration tests — fixtures for real Supabase test database.

Uses TEST_DATABASE_URL from .env to connect to a dedicated empty Supabase project.
Each test runs inside a transaction that is ROLLED BACK — no data persists.
"""

import json
import os

import asyncpg
from dotenv import load_dotenv

# Load .env to get TEST_DATABASE_URL from project root
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

# Shared test user ID constant for all integration tests
TEST_USER_ID = 999999


def pytest_collection_modifyitems(config, items):
    """Skip integration tests if TEST_DATABASE_URL is not set."""
    if TEST_DATABASE_URL:
        return
    skip_marker = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def test_db_url():
    """Return the test database URL or skip."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    return TEST_DATABASE_URL


@pytest.fixture
def test_user_id():
    """Provide the standard test user ID."""
    return TEST_USER_ID


@pytest.fixture
async def db_conn(test_db_url):
    """Provide a transactional DB connection that auto-rollbacks.

    - Connects to the test Supabase project
    - Starts a transaction
    - Yields the connection for test use
    - Rolls back ALL changes after test completes
    """
    conn = await asyncpg.connect(test_db_url, statement_cache_size=0)
    # Register JSONB codec to match production db_manager behavior
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    # Ensure schema is up-to-date for integration tests (idempotent).
    # These mirror production migrations that may not yet be applied to the test DB.
    await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS ltm_enabled BOOLEAN DEFAULT TRUE")
    await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS branch_id INTEGER")
    await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS temperature FLOAT")
    await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS voice_id TEXT")
    await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS tts_temperature FLOAT")
    await conn.execute("ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS rlhf_negative_count INTEGER DEFAULT 0")
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        try:
            # Cancel any in-flight operation that would block rollback.
            # This can happen when xdist defers fixture teardown while a
            # query from a prior test is still executing on this connection.
            if conn.is_in_transaction():
                await conn.reset(timeout=5.0)
            else:
                await tx.rollback()
        except Exception:
            pass  # Best-effort cleanup — connection will be closed below
        finally:
            await conn.close()


@pytest.fixture(autouse=True)
def force_test_db_conn(db_conn, monkeypatch):
    """Mock the global db_manager to always use the transactional db_conn.

    This ensures that even when the business logic pulls a connection from the pool,
    it receives the isolated connection bound to the test's transaction.
    """
    import contextlib

    from app.database import db_manager

    class TransactionalPool:
        _closed = False

        @contextlib.asynccontextmanager
        async def acquire(self):
            yield db_conn

        async def close(self):
            pass

    monkeypatch.setattr(db_manager, "pool", TransactionalPool())


@pytest.fixture
async def db_conn_with_user(db_conn):
    """Provide a DB connection with a pre-inserted test user (user_id=999999).

    Useful for tests that need FK references to users table.
    """
    await db_conn.execute(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, $2)",
        TEST_USER_ID,
        1,
    )
    return db_conn


@pytest.fixture
async def db_conn_with_key(db_conn_with_user):
    """Provide a DB connection with a pre-inserted test API key.

    Returns (connection, key_hash) for tests that need FK references to api_keys.
    """
    import hashlib

    test_key = "test-gemini-key-12345"
    key_hash = hashlib.sha256(test_key.encode()).hexdigest()[:16]

    await db_conn_with_user.execute(
        "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
        test_key,
        key_hash,
    )
    return db_conn_with_user, key_hash


@pytest.fixture
async def db_conn_with_metrics(db_conn_with_user):
    """Provide a DB connection with a pre-inserted user_metrics row for today.

    Useful for testing stats queries that need existing metric data.
    """
    await db_conn_with_user.execute(
        """INSERT INTO user_metrics (user_id, metric_date, request_count, model_usage)
           VALUES ($1, CURRENT_DATE, 10, '{"gemini-2.5-flash": 7, "gemini-3.1-flash-lite": 3}'::jsonb)""",
        TEST_USER_ID,
    )
    return db_conn_with_user
