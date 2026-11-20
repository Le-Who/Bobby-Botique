import pytest

pytestmark = pytest.mark.integration
"""Integration tests for memory repository and vector search logic."""

from unittest.mock import AsyncMock, patch

import pytest

from app.database import db_query
from app.repos.memory import EMBEDDING_DIMENSION, search_memories, store_memory


@pytest.fixture
def mock_embedding():
    """Mock the Gemini API embedding generation to avoid network calls in DB test."""
    with patch("app.repos.memory._get_embedding", new_callable=AsyncMock) as m_embed:
        m_embed.return_value = [0.1] * EMBEDDING_DIMENSION
        yield m_embed


@pytest.mark.asyncio
@pytest.mark.integration
async def test_memory_storage_and_vector_search(db_conn_with_key, mock_embedding):
    """
    Risk Covered: Memories not stored or cosine similarity returning bad matches.
    Level: Integration (Requires Postgres + pgvector)
    """
    conn, api_key = db_conn_with_key
    user_id = 999999
    await conn.execute("INSERT INTO chats (user_id, ltm_enabled) VALUES ($1, TRUE)", user_id)

    memory_text = "I have a dog named Rex and a cat named Whiskers."
    memory_id = await store_memory(user_id, memory_text, api_key, source_type="conversation")
    assert memory_id is not None

    rows = await db_query(
        "SELECT content FROM long_term_memory WHERE user_id = $1",
        (user_id,),
        conn=conn,
    )
    assert len(rows) == 1
    assert rows[0]["content"] == memory_text

    search_query = "What pets do I own?"
    results = await search_memories(
        user_id,
        search_query,
        api_key,
        limit=5,
        min_similarity=0.0,
    )

    assert len(results) == 1
    assert results[0]["content"] == memory_text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_memory_storage_enforces_max_limit(db_conn_with_key, mock_embedding):
    """
    Risk Covered: Unbounded memory growth filling up Postgres disk.
    Level: Integration.
    """
    conn, api_key = db_conn_with_key
    user_id = 999999
    await conn.execute("INSERT INTO chats (user_id, ltm_enabled) VALUES ($1, TRUE)", user_id)

    with patch("app.repos.memory.MAX_MEMORIES_PER_USER", 2):
        stored_ids = []
        for i in range(3):
            stored_ids.append(await store_memory(user_id, f"Memory number {i}", api_key))

        assert all(memory_id is not None for memory_id in stored_ids)

        rows = await db_query(
            "SELECT count(*) as cnt FROM long_term_memory WHERE user_id = $1",
            (user_id,),
            conn=conn,
        )

        assert rows[0]["cnt"] <= 2
