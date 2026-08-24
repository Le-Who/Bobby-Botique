"""
E2E test conftest — database fixtures for end-to-end tests.

DB fixtures are shared from tests/integration/conftest.py via explicit import
(NOT via pytest_plugins, which would make autouse fixtures global).
"""

import contextlib
import json
import os
from urllib.parse import urlsplit

import asyncpg
import pytest
from dotenv import dotenv_values

_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
_FILE_ENV = dotenv_values(_ENV_PATH)
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or _FILE_ENV.get("TEST_DATABASE_URL")
_PRODUCTION_DATABASE_URL = os.getenv("GEMAIBOT_TEST_ORIGINAL_DATABASE_URL") or _FILE_ENV.get("DATABASE_URL")


def _database_identity(value: str | None):
    if not value:
        return None
    parsed = urlsplit(value)
    return parsed.hostname, parsed.port or 5432, parsed.path.rstrip("/")


if TEST_DATABASE_URL and _database_identity(TEST_DATABASE_URL) == _database_identity(_PRODUCTION_DATABASE_URL):
    raise RuntimeError("TEST_DATABASE_URL resolves to the production database target")

# Shared test user ID constant
TEST_USER_ID = 999999


def pytest_collection_modifyitems(config, items):
    """Skip e2e tests if TEST_DATABASE_URL is not configured."""
    if TEST_DATABASE_URL:
        return
    skip_marker = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    e2e_dir = os.path.join(os.path.dirname(__file__))
    for item in items:
        if str(item.fspath).startswith(e2e_dir):
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def test_db_url():
    """Return the test database URL or skip."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    return TEST_DATABASE_URL


@pytest.fixture
def test_user_id():
    return TEST_USER_ID


@pytest.fixture
async def db_conn(test_db_url):
    """Transactional DB connection that auto-rollbacks after each test."""
    conn = await asyncpg.connect(test_db_url, statement_cache_size=0)
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS ltm_enabled BOOLEAN DEFAULT TRUE")
        await conn.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS branch_id INTEGER")
        yield conn
    finally:
        try:
            if conn.is_in_transaction():
                await conn.reset(timeout=5.0)
            else:
                await tx.rollback()
        except Exception:
            pass
        finally:
            await conn.close()


@pytest.fixture
def force_test_db_conn(db_conn, monkeypatch):
    """Redirect db_manager to the transactional test connection.

    Not autouse=True — e2e tests opt in explicitly to avoid the global
    session contamination that pytest_plugins caused previously.
    """
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
    await db_conn.execute(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, $2)",
        TEST_USER_ID,
        1,
    )
    return db_conn


@pytest.fixture
async def db_conn_with_key(db_conn_with_user):
    import hashlib

    test_key = "test-gemini-key-12345"
    key_hash = hashlib.sha256(test_key.encode()).hexdigest()[:16]
    await db_conn_with_user.execute(
        "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
        test_key,
        key_hash,
    )
    return db_conn_with_user, key_hash
