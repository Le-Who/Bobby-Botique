# /app/repos/memory.py
"""Long-term memory repository — semantic search over past conversations.

Uses pgvector for embedding storage and HNSW-indexed cosine similarity search.
Embeddings are generated via Gemini's embedding API (gemini-embedding-001, 3072-dim).
"""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from google.genai import types

from app.config import settings
from app.database import (
    clear_user_context,
    db_execute_many,
    db_manager,
    db_query,
    set_user_context,
)
from app.providers.gemini import get_cached_genai_client

# ── Constants ────────────────────────────────────────────────────────────────

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768
MAX_MEMORIES_PER_USER = 500
DEFAULT_MEMORY_TTL_DAYS = 90

# Cached flag: True if pg_trgm extension is available in this database
_trgm_available: bool | None = None


async def _get_embedding(
    content: str | list[Any],
    api_key: str,
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> list[float] | None:
    """Generate an embedding for the given text or multimodal payload.

    Uses Gemini's gemini-embedding-2-preview model (768-dim, pre-normalized).
    The task_type should be:
      - RETRIEVAL_DOCUMENT when storing content for later retrieval
      - RETRIEVAL_QUERY when searching for similar content

    Returns None on failure (non-critical — memory just won't be stored).
    """
    try:
        client = get_cached_genai_client(api_key)  # Reuse cached client (HTTP/2 multiplexing)

        # Apply truncation for long text context up to ~30,000 chars (model supports 8192 tokens)
        payload = content[:30000] if isinstance(content, str) else content

        result = await client.aio.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=payload,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBEDDING_DIMENSION,
            ),
        )
        if result and result.embeddings:
            return result.embeddings[0].values
    except Exception as e:
        logging.warning("Embedding generation failed: %s", e)
        # Emit metric for observability (Change 2: LTM failure tracking)
        try:
            from app.metrics import metrics_collector

            await metrics_collector.record_error("ltm_embedding_fail", str(e))
        except Exception:
            pass  # Metrics emission must not block
    return None


async def store_memory(
    user_id: int,
    content: str | list[Any],
    api_key: str,
    *,
    source_type: str = "conversation",
    metadata: dict[str, Any] | None = None,
    ttl_days: int = DEFAULT_MEMORY_TTL_DAYS,
) -> int | None:
    """Store a memory with its embedding.

    Args:
        user_id: Owner of the memory.
        content: Text or multimodal payload to embed and store.
        api_key: Gemini API key for embedding generation.
        source_type: 'conversation', 'summary', 'document', etc.
        metadata: Additional JSON metadata.
        ttl_days: Days until auto-expiration (0 = no expiry).

    Returns:
        Memory ID on success, None on failure.
    """
    if isinstance(content, str):
        if not content or len(content.strip()) < 10:
            return None
        db_text_content = content[:32000]
    else:
        if not content:
            return None
        # Provide a text representation for DB storage by picking out strings
        strings = [str(c) for c in content if isinstance(c, str)]
        db_text_content = (" ".join(strings) or "<Multimodal Memory>")[:32000]

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
                        db_text_content,  # Truncated or serialized to text for Postgres
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


async def _check_trgm_available() -> bool:
    """Check (and cache) whether pg_trgm extension is installed."""
    global _trgm_available
    if _trgm_available is not None:
        return _trgm_available
    try:
        result = await db_query(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'",
            (),
        )
        _trgm_available = bool(result)
    except Exception:
        _trgm_available = False
    return _trgm_available


async def search_memories(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 5,
    min_similarity: float = 0.5,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity with optional keyword boost (RRF).

    Uses Reciprocal Rank Fusion to combine pgvector cosine similarity with
    pg_trgm keyword matching.  Falls back to pure semantic search if pg_trgm
    is not installed.

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
    use_trgm = await _check_trgm_available()

    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                if use_trgm:
                    # RRF hybrid: cosine similarity + keyword trigram
                    results = await db_query(
                        """
                        WITH semantic AS (
                            SELECT id, content, source_type, metadata, created_at,
                                   1 - (embedding <=> $2::halfvec) AS sim,
                                   ROW_NUMBER() OVER (ORDER BY embedding <=> $2::halfvec) AS rank_s
                            FROM long_term_memory
                            WHERE user_id = $1
                              AND (expires_at IS NULL OR expires_at > now())
                            ORDER BY embedding <=> $2::halfvec
                            LIMIT 20
                        ),
                        keyword AS (
                            SELECT id,
                                   ROW_NUMBER() OVER (ORDER BY similarity(content, $5) DESC) AS rank_k
                            FROM long_term_memory
                            WHERE user_id = $1
                              AND (expires_at IS NULL OR expires_at > now())
                              AND content % $5
                            ORDER BY similarity(content, $5) DESC
                            LIMIT 20
                        )
                        SELECT s.id, s.content, s.source_type, s.metadata, s.created_at,
                               s.sim,
                               (1.0/(60+s.rank_s)) + COALESCE(1.0/(60+k.rank_k), 0) AS rrf_score
                        FROM semantic s
                        LEFT JOIN keyword k ON s.id = k.id
                        WHERE s.sim >= $3
                        ORDER BY rrf_score DESC
                        LIMIT $4
                        """,
                        (user_id, embedding_str, min_similarity, limit, query[:500]),
                        conn=conn,
                    )
                else:
                    # Pure semantic fallback
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
                        "similarity": float(r.get("sim", r.get("similarity", 0))),
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


async def search_memories_with_graph(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 5,
    min_similarity: float = 0.5,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Graph-augmented memory search: vector search + 1-hop graph traversal.

    Combines standard search_memories results with related entities/relations
    from the knowledge graph. Returns (memories, graph_triples) where
    graph_triples are human-readable strings like "Python — uses → FastAPI".
    """
    # 1. Standard vector search for memories
    memories = await search_memories(user_id, query, api_key, limit=limit, min_similarity=min_similarity)

    # 2. Graph traversal: find related entities
    graph_triples: list[str] = []
    try:
        query_embedding = await _get_embedding(query, api_key, task_type="RETRIEVAL_QUERY")
        if query_embedding is None:
            return memories, graph_triples

        embedding_str = f"[{','.join(str(v) for v in query_embedding)}]"

        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                # Find top-K similar entity nodes
                nodes = await db_query(
                    """
                    SELECT id, entity_name, entity_type, description,
                           1 - (embedding <=> $2::halfvec) AS sim
                    FROM memory_nodes
                    WHERE user_id = $1
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> $2::halfvec
                    LIMIT 5
                    """,
                    (user_id, embedding_str),
                    conn=conn,
                )

                if not nodes:
                    return memories, graph_triples

                # Collect node IDs for 1-hop traversal
                relevant_ids = [n["id"] for n in nodes if float(n.get("sim", 0)) >= 0.4]
                if not relevant_ids:
                    return memories, graph_triples

                # 1-hop: get edges connected to these nodes
                edges = await db_query(
                    """
                    SELECT
                        src.entity_name AS from_name,
                        e.predicate,
                        tgt.entity_name AS to_name,
                        e.weight
                    FROM memory_edges e
                    JOIN memory_nodes src ON e.source_node = src.id
                    JOIN memory_nodes tgt ON e.target_node = tgt.id
                    WHERE e.user_id = $1
                      AND (e.source_node = ANY($2::uuid[]) OR e.target_node = ANY($2::uuid[]))
                    ORDER BY e.weight DESC
                    LIMIT 10
                    """,
                    (user_id, relevant_ids),
                    conn=conn,
                )

                for edge in edges or []:
                    graph_triples.append(f"{edge['from_name']} — {edge['predicate']} → {edge['to_name']}")

                logging.debug(
                    "Graph search for user %d: %d nodes, %d edges found",
                    user_id,
                    len(relevant_ids),
                    len(graph_triples),
                )
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.warning("Graph traversal failed (non-critical): %s", e)

    return memories, graph_triples


async def delete_user_memories(user_id: int) -> int:
    """Delete all memories + graph data for a user. Returns count of deleted memory records."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                async with conn.transaction():
                    # Delete graph data first (edges cascade from nodes via FK)
                    await conn.execute(
                        "DELETE FROM memory_nodes WHERE user_id = $1",
                        user_id,
                    )

                    # Delete long-term memories
                    result = await db_query(
                        "DELETE FROM long_term_memory WHERE user_id = $1 RETURNING id",
                        (user_id,),
                        conn=conn,
                    )
                    count = len(result) if result else 0
                    logging.info("Deleted %d memories + graph data for user %d", count, user_id)
                    return count
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Failed to delete memories for user %d: %s", user_id, e, exc_info=True)
        return 0


async def delete_memory(user_id: int, memory_id: int) -> bool:
    """Delete a single memory by ID (RLS-safe: scoped to user_id)."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                result = await db_query(
                    "DELETE FROM long_term_memory WHERE id = $1 AND user_id = $2 RETURNING id",
                    (memory_id, user_id),
                    conn=conn,
                )
                return bool(result)
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Failed to delete memory %d for user %d: %s", memory_id, user_id, e, exc_info=True)
        return False


async def list_memories(
    user_id: int,
    *,
    offset: int = 0,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """List memories for a user, ordered by newest first (for /memory UI)."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                results = await db_query(
                    """
                    SELECT id, content, source_type, created_at
                    FROM long_term_memory
                    WHERE user_id = $1
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    (user_id, limit, offset),
                    conn=conn,
                )
                return [
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "source_type": r["source_type"],
                        "created_at": r["created_at"],
                    }
                    for r in (results or [])
                ]
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Failed to list memories for user %d: %s", user_id, e, exc_info=True)
        return []


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
                        COUNT(*) FILTER (WHERE source_type IN ('conversation', 'user_intent')) as memories,
                        COUNT(*) FILTER (WHERE source_type = 'consolidated') as consolidated,
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
                        "total_memories": r["total"],
                        "raw_memories": r["memories"],
                        "consolidated": r["consolidated"],
                        "oldest": r["oldest"],
                        "newest": r["newest"],
                        "limit": MAX_MEMORIES_PER_USER,
                    }
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Memory stats failed for user %d: %s", user_id, e, exc_info=True)

    return {"total": 0, "limit": MAX_MEMORIES_PER_USER}
