from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.database import db_manager
from app.repos.memory import EMBEDDING_DIMENSION

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_pgvector_large_content_truncation_and_preflight_fail_closed(db_conn):
    """
    Large text is embedded whole and stored once with a 32,000-character cap.
    A failed consent preflight must return None without invoking embeddings.
    """
    from app.repos.memory import store_memory

    # 1. Arrange: Create a user and a very large memory text
    user_id = 9000001
    await db_conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
    await db_conn.execute("INSERT INTO chats (user_id, ltm_enabled) VALUES ($1, TRUE)", user_id)

    large_text = "Big long memory content. " * 2000

    # 2. Act: Store memory. we need to mock the embeddings API to just return a dummy vector
    with patch("app.repos.memory._get_embedding", new_callable=AsyncMock) as mock_embed:
        mock_embed.return_value = [0.1] * EMBEDDING_DIMENSION

        # We store it
        success_id = await store_memory(
            user_id=user_id,
            content=large_text,
            api_key="dummy_key",
            source_type="test_document",
        )

    # 3. Assert: Verify it succeeded and the DB has the entry
    assert success_id is not None
    mock_embed.assert_awaited_once_with(large_text, "dummy_key")

    rows = await db_conn.fetch("SELECT id, content FROM long_term_memory WHERE user_id = $1", user_id)
    assert [(row["id"], row["content"]) for row in rows] == [(success_id, large_text[:32000])]

    # 4. Act: Test fallback on database error
    # We patch the acquire to throw an error
    @asynccontextmanager
    async def fail_acquire():
        raise ConnectionError("isolated test preflight unavailable")
        yield None

    with (
        patch.object(db_manager.pool, "acquire", fail_acquire),
        patch("app.repos.memory._get_embedding", new_callable=AsyncMock) as mock_embed2,
    ):
        mock_embed2.return_value = [0.1] * EMBEDDING_DIMENSION
        success_fail_id = await store_memory(
            user_id=user_id,
            content="Small text",
            api_key="dummy_key",
            source_type="test_document",
        )

    # 5. Assert fallback
    assert success_fail_id is None
    mock_embed2.assert_not_awaited()
    assert await db_conn.fetchval("SELECT COUNT(*) FROM long_term_memory WHERE user_id = $1", user_id) == 1
