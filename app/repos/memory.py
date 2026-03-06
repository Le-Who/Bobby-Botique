# /app/repos/memory.py
"""Long-term memory repository — semantic search over past conversations.

Uses pgvector for embedding storage and HNSW-indexed cosine similarity search.
Embeddings are generated via Gemini's embedding API (text-embedding-004, 768-dim).
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from google import genai
from google.genai import types

from app.config import settings
from app.database import clear_user_context, db_execute_many, db_manager, db_query, set_user_context

# ── Constants ────────────────────────────────────────────────────────────────

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 3072
MAX_MEMORIES_PER_USER = 500
DEFAULT_MEMORY_TTL_DAYS = 90


async def _get_embedding(
    text: str,
    api_key: str,
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> list[float] | None:
    """Generate an embedding for the given text.

    Uses Gemini's gemini-embedding-001 model (3072-dim, pre-normalized).
    The task_type should be:
      - RETRIEVAL_DOCUMENT when storing content for later retrieval
      - RETRIEVAL_QUERY when searching for similar content

    Returns None on failure (non-critical — memory just won't be stored).
    """
    try:
        client = genai.Client(api_key=api_key)
        result = await client.aio.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text[:8000],  # Truncate to model limit
            config=types.EmbedContentConfig(task_type=task_type),
        )
        if result and result.embeddings:
            return result.embeddings[0].values
    except Exception as e:
        logging.warning("Embedding generation failed: %s", e)
    return None


async def store_memory(
    user_id: int,
    content: str,
    api_key: str,
    *,
    source_type: str = "conversation",
    metadata: dict[str, Any] | None = None,
    ttl_days: int = DEFAULT_MEMORY_TTL_DAYS,
) -> int | None:
    """Store a memory with its embedding.

    Args:
        user_id: Owner of the memory.
        content: Text content to embed and store.
        api_key: Gemini API key for embedding generation.
        source_type: 'conversation', 'summary', 'document', etc.
        metadata: Additional JSON metadata.
        ttl_days: Days until auto-expiration (0 = no expiry).

    Returns:
        Memory ID on success, None on failure.
    """
    if not content or len(content.strip()) < 10:
        return None

    embedding = await _get_embedding(content, api_key)
    if embedding is None:
        logging.warning("Skipping memory storage — embedding generation failed")
        return None

    expires_at = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days > 0 else None

    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                # Check memory count limit
                count_result = await db_query(
                    "SELECT COUNT(*) as cnt FROM long_term_memory WHERE user_id = $1",
                    (user_id,),
                    conn=conn,
                )
                if count_result and count_result[0]["cnt"] >= MAX_MEMORIES_PER_USER:
                    # Delete oldest to make room
                    await db_query(
                        "DELETE FROM long_term_memory WHERE id IN ("
                        "  SELECT id FROM long_term_memory WHERE user_id = $1"
                        "  ORDER BY created_at ASC LIMIT 10"
                        ")",
                        (user_id,),
                        conn=conn,
                    )

                result = await db_query(
                    "INSERT INTO long_term_memory (user_id, content, embedding, source_type, metadata, expires_at) "
                    "VALUES ($1, $2, $3::halfvec, $4, $5::jsonb, $6) RETURNING id",
                    (
                        user_id,
                        content[:10000],  # Truncate
                        f"[{','.join(str(v) for v in embedding)}]",
                        source_type,
                        __import__("json").dumps(metadata or {}),
                        expires_at,
                    ),
                    conn=conn,
                )
                if result:
                    return result[0]["id"]
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Failed to store memory for user %d: %s", user_id, e, exc_info=True)
    return None


async def search_memories(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 5,
    min_similarity: float = 0.5,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity.

    Args:
        user_id: Owner of the memories.
        query: Search query text.
        api_key: Gemini API key for query embedding.
        limit: Maximum results.
        min_similarity: Minimum cosine similarity threshold (0-1).

    Returns:
        List of dicts with 'id', 'content', 'similarity', 'source_type', 'created_at'.
    """
    query_embedding = await _get_embedding(query, api_key, task_type="RETRIEVAL_QUERY")
    if query_embedding is None:
        return []

    embedding_str = f"[{','.join(str(v) for v in query_embedding)}]"

    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                results = await db_query(
                    """
                    SELECT id, content, source_type, metadata, created_at,
                           1 - (embedding <=> $2::halfvec) AS similarity
                    FROM long_term_memory
                    WHERE user_id = $1
                      AND (expires_at IS NULL OR expires_at > now())
                      AND 1 - (embedding <=> $2::halfvec) >= $3
                    ORDER BY similarity DESC
                    LIMIT $4
                    """,
                    (user_id, embedding_str, min_similarity, limit),
                    conn=conn,
                )
                return [
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "similarity": float(r["similarity"]),
                        "source_type": r["source_type"],
                        "created_at": r["created_at"],
                    }
                    for r in (results or [])
                ]
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Memory search failed for user %d: %s", user_id, e, exc_info=True)
        return []


async def delete_user_memories(user_id: int) -> int:
    """Delete all memories for a user. Returns count of deleted records."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                result = await db_query(
                    "DELETE FROM long_term_memory WHERE user_id = $1 RETURNING id",
                    (user_id,),
                    conn=conn,
                )
                count = len(result) if result else 0
                logging.info("Deleted %d memories for user %d", count, user_id)
                return count
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Failed to delete memories for user %d: %s", user_id, e, exc_info=True)
        return 0


async def cleanup_expired_memories() -> int:
    """Delete all expired memories across all users. Returns count deleted."""
    try:
        result = await db_query(
            "DELETE FROM long_term_memory WHERE expires_at IS NOT NULL AND expires_at < now() RETURNING id",
            (),
        )
        count = len(result) if result else 0
        if count > 0:
            logging.info("Cleaned up %d expired memories", count)
        return count
    except Exception as e:
        logging.error("Memory cleanup failed: %s", e, exc_info=True)
        return 0


async def get_memory_stats(user_id: int) -> dict[str, Any]:
    """Get memory usage stats for a user."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                result = await db_query(
                    """
                    SELECT
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE source_type = 'conversation') as conversations,
                        COUNT(*) FILTER (WHERE source_type = 'summary') as summaries,
                        MIN(created_at) as oldest,
                        MAX(created_at) as newest
                    FROM long_term_memory
                    WHERE user_id = $1
                      AND (expires_at IS NULL OR expires_at > now())
                    """,
                    (user_id,),
                    conn=conn,
                )
                if result:
                    r = result[0]
                    return {
                        "total": r["total"],
                        "conversations": r["conversations"],
                        "summaries": r["summaries"],
                        "oldest": r["oldest"],
                        "newest": r["newest"],
                        "limit": MAX_MEMORIES_PER_USER,
                    }
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Memory stats failed for user %d: %s", user_id, e, exc_info=True)

    return {"total": 0, "limit": MAX_MEMORIES_PER_USER}
