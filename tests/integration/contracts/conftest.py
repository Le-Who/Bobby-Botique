"""Independent connections in a disposable clone of the migrated test database.

Unlike the parent rollback fixture, this boundary permits real commits and pool
concurrency. It requires explicit ephemeral-test authorization and drops only the
fresh database whose generated name this fixture owns.
"""

import asyncio
import json
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest


@pytest.fixture
async def contract_database_url(test_db_url):
    if os.getenv("GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL", "").lower() != "true":
        pytest.skip("Real-commit contracts require an explicitly ephemeral test database")
    parsed = urlsplit(test_db_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Disposable database contracts require a local isolated test server")
    template = parsed.path.removeprefix("/")
    if not template or '"' in template or "\x00" in template:
        pytest.fail("Invalid test database template name")
    owned_name = "gemaibot_test_contract_" + uuid.uuid4().hex
    admin_url = urlunsplit(parsed._replace(path="/postgres"))
    admin = await asyncpg.connect(admin_url, statement_cache_size=0)
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{owned_name}" TEMPLATE "{template}"')
        created = True
        yield urlunsplit(parsed._replace(path="/" + owned_name))
    finally:
        try:
            if created:
                await asyncio.wait_for(admin.execute(f'DROP DATABASE "{owned_name}" WITH (FORCE)'), timeout=10)
        finally:
            await admin.close(timeout=5)


@pytest.fixture
async def contract_pool(contract_database_url):
    async def initialize(connection):
        await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    pool = await asyncpg.create_pool(
        contract_database_url,
        min_size=2,
        max_size=8,
        statement_cache_size=0,
        init=initialize,
    )
    try:
        yield pool
    finally:
        await asyncio.wait_for(pool.close(), timeout=10)


@pytest.fixture(autouse=True)
def force_test_db_conn(contract_pool, monkeypatch):
    """Override the parent's single-connection pool only in this directory."""
    from app.database import db_manager

    monkeypatch.setattr(db_manager, "pool", contract_pool)


@pytest.fixture
async def db_conn(contract_pool):
    async with contract_pool.acquire() as connection:
        yield connection
