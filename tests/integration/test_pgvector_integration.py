import pytest

pytestmark = pytest.mark.integration
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.database import db_manager

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_db_manager(db_conn):
    """Patches db_manager to yield the test transactional connection"""

    @asynccontextmanager
    async def mock_acquire():
        yield db_conn

    with patch.object(db_manager, "pool", create=True) as mock_pool:
        mock_pool.acquire = mock_acquire
        yield mock_pool


@pytest.mark.asyncio
async def test_pgvector_chunking_and_fallback(db_conn, mock_db_manager):
    """
    TC-002: Ensure large text insertion into pgvector gets properly chunked
    and handles potential database connection/dimension errors gracefully.
    """
    from app.database import db_query
    from app.repos.memory import store_memory

    # 1. Arrange: Create a user and a very large memory text
    user_id = 9000001
    await db_conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)

    # A single string with over 20,000 characters
    large_text = "Big long memory chunk. " * 1000

    # 2. Act: Store memory. we need to mock the embeddings API to just return a dummy vector
    with patch("app.repos.memory._get_embedding", new_callable=AsyncMock) as mock_embed:
        # Mocking an embedding array of size 3072 since the schema requires halfvec(3072)
        mock_embed.return_value = [0.1] * 3072

        # We store it
        success_id = await store_memory(
            user_id=user_id,
            content=large_text,
            api_key="dummy_key",
            source_type="test_document",
        )

    # 3. Assert: Verify it succeeded and the DB has the entry
    assert success_id is not None, "Expected memory storage to succeed via chunks"

    count = await db_conn.fetchval("SELECT COUNT(*) FROM long_term_memory WHERE user_id = $1", user_id)
    assert count >= 1, "Expected at least 1 memory chunk stored"

    # 4. Act: Test fallback on database error
    # We patch the acquire to throw an error
    @asynccontextmanager
    async def fail_acquire():
        raise Exception("DB Con Error")
        yield None

    mock_db_manager.acquire = fail_acquire

    with patch("app.repos.memory._get_embedding", new_callable=AsyncMock) as mock_embed2:
        mock_embed2.return_value = [0.1] * 3072
        success_fail_id = await store_memory(
            user_id=user_id,
            content="Small text",
            api_key="dummy_key",
            source_type="test_document",
        )

    # 5. Assert fallback
    assert success_fail_id is None, "Expected fallback gracefully (returning None) instead of raising exception"
