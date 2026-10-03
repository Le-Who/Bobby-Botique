"""Connection ownership shared by isolated PostgreSQL test fixtures."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg


@asynccontextmanager
async def transactional_connection(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(database_url, statement_cache_size=0)
    try:
        await connection.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        transaction = connection.transaction()
        await transaction.start()
        try:
            yield connection
        finally:
            await asyncio.wait_for(transaction.rollback(), timeout=5.0)
    finally:
        await connection.close(timeout=5.0)
