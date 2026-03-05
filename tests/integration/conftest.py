"""Integration tests — fixtures for real Supabase test database.

Uses TEST_DATABASE_URL from .env to connect to a dedicated empty Supabase project.
Each test runs inside a transaction that is ROLLED BACK — no data persists.
"""

import os

import asyncpg
import pytest
from dotenv import load_dotenv

# Load .env to get TEST_DATABASE_URL
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


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
async def db_conn(test_db_url):
    """Provide a transactional DB connection that auto-rollbacks.

    - Connects to the test Supabase project
    - Starts a transaction
    - Yields the connection for test use
    - Rolls back ALL changes after test completes
    """
    conn = await asyncpg.connect(test_db_url, statement_cache_size=0)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
async def db_conn_with_user(db_conn):
    """Provide a DB connection with a pre-inserted test user (user_id=999999).

    Useful for tests that need FK references to users table.
    """
    await db_conn.execute(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, $2)",
        999999,
        1,
    )
    return db_conn
