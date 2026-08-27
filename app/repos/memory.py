# /app/repos/memory.py
"""Long-term memory repository — semantic search over past conversations.

Uses pgvector for embedding storage and HNSW-indexed cosine similarity search.
Embeddings are generated via Gemini's embedding API (gemini-embedding-2-preview, 768-dim halfvec).
"""

import logging
import re
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from cachetools import TTLCache
from google.genai import types

from app.database import (
    db_manager,
    db_query,
    set_user_context,
)
from app.providers.gemini import get_cached_genai_client

# ── Constants (re-exported from memory_config for backward compatibility) ─────
from app.repos.memory_config import (
    DEFAULT_MEMORY_TTL_DAYS,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    MAX_MEMORIES_PER_USER,
    QUERY_EXPANSION_MODEL,
)
from app.utils.json_compat import json

__all__ = [
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
    "QUERY_EXPANSION_MODEL",
    "MAX_MEMORIES_PER_USER",
    "DEFAULT_MEMORY_TTL_DAYS",
    "is_ltm_write_enabled",
]

# Cached flag: True if pg_trgm extension is available in this database
_trgm_available: bool | None = None

# Response-bound attribution. ContextVar keeps concurrent retrieval tasks isolated;
# the bounded TTL cache bridges the retrieval task to a later Telegram callback.
_current_retrieved_edge_ids: ContextVar[tuple[int, tuple[int, ...]] | None] = ContextVar(
    "current_retrieved_edge_ids",
    default=None,
)
_response_retrieved_edge_ids: TTLCache[tuple[int, int], tuple[int, ...]] = TTLCache(maxsize=4096, ttl=3600)


# ── Query Intent Gate ────────────────────────────────────────────────────────
# Compiled once at import time.  Matches trivial conversational inputs where
# burning a Flash-Lite LLM call for query expansion is pure waste.
_TRIVIAL_QUERY_RE = re.compile(
    r"^(?:"
    r"привет|здравствуй|хай|хей|hello|hi|hey|yo"
    r"|спасибо|thanks|thx|ок|ok|ладно|хорошо|понял|ясно|ага|угу"
    r"|да|нет|yes|no|ну|ой|ого|вау|wow|lol"
    r"|пока|bye|👋|👍|👎|❤️|🔥"
    r")\s*[!?.…]*$",
    re.IGNORECASE,
)

# Minimum query length that justifies an LLM expansion call.
# Anything shorter is either a greeting or too terse to meaningfully expand.
_MIN_EXPANSION_LENGTH = 12


def _should_expand_query(query: str) -> bool:
    """Determine whether a user query warrants LLM-based expansion.

    Returns False for trivial conversational inputs (greetings, one-word
    confirmations, emoji-only messages) where burning a Flash-Lite call
    solely to rewrite "hi" → "greeting hello conversation" is wasteful.

    The heuristic is intentionally conservative: when in doubt, expand.
    """
    stripped = query.strip()
    if len(stripped) < _MIN_EXPANSION_LENGTH:
        return False
    return not _TRIVIAL_QUERY_RE.match(stripped)


async def expand_query_with_llm(query: str, api_key: str) -> str:
    """Expand a vague / ambiguous user query into a concise keyword-rich search phrase.

    Uses a cheap Flash-Lite call (~200ms) to re-phrase questions like
    "That framework I mentioned yesterday?" into a phrase like
    "Python web framework project FastAPI" that embeds near actual memories.

    Falls back to the original query on any error so the pipeline never blocks.

    Args:
        query: Original user message text.
        api_key: Gemini API key.

    Returns:
        Expanded search phrase (usually shorter / more keyword-dense).
    """
    try:
        from google.genai import types as _types

        client = get_cached_genai_client(api_key)
        prompt = (
            "You are a memory search assistant. "
            "Rewrite the following user query as a concise, keyword-rich search phrase "
            "(maximum 20 words) that will best match stored personal facts about the user. "
            "Output ONLY the search phrase, nothing else.\n\n"
            f"User query: {query[:500]}"
        )
        resp = await client.aio.models.generate_content(
            model=QUERY_EXPANSION_MODEL,
            contents=prompt,
            config=_types.GenerateContentConfig(temperature=0.0, max_output_tokens=60),
        )
        expanded = (resp.text or "").strip().strip('"')
        if expanded and len(expanded) > 3:
            logging.debug("Query expansion: %r -> %r", query[:80], expanded)
            return expanded
    except Exception as exc:
        logging.debug("Query expansion failed (non-critical): %s", exc)
    return query


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
    On 400 INVALID_ARGUMENT (revoked/invalid key), attempts one key rotation.
    """
    import hashlib

    # Apply truncation for long text context up to ~30,000 chars (model supports 8192 tokens)
    payload = content[:30000] if isinstance(content, str) else content

    current_key: str | None = api_key
    if current_key and (current_key.startswith("sk-") or "opencode" in current_key.lower()):
        current_key = None  # Force immediate resolution of a native Gemini key

    failed_hashes: set[str] = set()

    for _attempt in range(2):  # One retry with a rotated key on 400
        if not current_key:
            from app.handlers.ai_core import _resolve_ai_request

            try:
                key_data, _, _ = await _resolve_ai_request(
                    EMBEDDING_MODEL, use_openrouter=False, excluded_key_hashes=failed_hashes
                )
                if key_data:
                    current_key = key_data["api_key"]
            except Exception:
                pass

        if not current_key:
            return None
        try:
            client = get_cached_genai_client(current_key)  # Reuse cached client (HTTP/2 multiplexing)
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
            error_str = str(e)
            is_invalid_key = "400" in error_str and ("invalid" in error_str.lower() or "api_key" in error_str.lower())
            logging.warning("Embedding generation failed (attempt %d): %s", _attempt + 1, e)

            if is_invalid_key and _attempt == 0:
                # Key is revoked/invalid — track hash and rotate to a fresh one
                key_hash = hashlib.sha256(current_key.encode()).hexdigest()[:16]
                failed_hashes.add(key_hash)
                try:
                    from app.handlers.ai_core import _resolve_ai_request

                    key_data, _, _ = await _resolve_ai_request(
                        EMBEDDING_MODEL,
                        use_openrouter=False,
                        excluded_key_hashes=failed_hashes,
                    )
                    if key_data:
                        current_key = key_data["api_key"]
                        continue  # Retry with the fresh key
                except Exception as resolve_exc:
                    logging.debug("Embedding key rotation failed (non-critical): %s", resolve_exc)

            # Emit metric for observability
            try:
                from app.metrics import metrics_collector

                await metrics_collector.record_error("ltm_embedding_fail", error_str)
            except Exception:
                pass  # Metrics emission must not block
            break

        return None

    return None


async def is_ltm_write_enabled(user_id: int, expected_epoch: int | None = None) -> bool:
    """Preflight durable LTM write consent before expensive external processing.

    This is an optimization/privacy boundary, not the commit authority: writers must
    still lock and recheck consent plus epoch in their mutation transaction. A missing
    legacy chat row preserves store_memory's enabled/epoch-zero compatibility contract.
    Database failures fail closed.
    """
    from app.repos.memory_consent import (
        is_private_data_snapshot_current,
        resolve_current_epoch,
    )

    epoch = expected_epoch
    if epoch is None:
        epoch = await resolve_current_epoch(user_id, require_ltm=True)
    return await is_private_data_snapshot_current(
        user_id,
        epoch,
        require_ltm=True,
    )


async def store_memory(
    user_id: int,
    content: str | list[Any],
    api_key: str,
    *,
    source_type: str = "conversation",
    metadata: dict[str, Any] | None = None,
    ttl_days: int = DEFAULT_MEMORY_TTL_DAYS,
    expected_epoch: int | None = None,
    wing: str | None = None,
    room: str | None = None,
    hall_type: str | None = None,
) -> int | None:
    """Store a memory with its embedding.

    Args:
        user_id: Owner of the memory.
        content: Text or multimodal payload to embed and store.
        api_key: Gemini API key for embedding generation.
        source_type: 'conversation', 'summary', 'document', etc.
        metadata: Additional JSON metadata.
        ttl_days: Days until auto-expiration (0 = no expiry).
        expected_epoch: Consent epoch captured when a background write was queued.
            A mismatched epoch makes the write a no-op. ``None`` preserves
            compatibility for synchronous/manual callers while still checking consent.
        wing: MemPalace wing classification (identity/projects/social/knowledge/temporal).
        room: MemPalace room within wing.
        hall_type: Content type (fact/opinion/event/plan/preference/habit).

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

    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    resolved_epoch = expected_epoch
    if resolved_epoch is None:
        # Compatibility for immediate/manual callers: bind them to the current
        # generation before any provider receives content. Background callers
        # must carry their captured epoch and are never silently rebound.
        resolved_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    if resolved_epoch is None:
        return None

    async with private_data_lease(
        user_id,
        resolved_epoch,
        purpose="ltm:store_embedding",
        require_ltm=True,
    ) as lease_acquired:
        if not lease_acquired:
            logging.debug("Skipping memory storage preflight for user %d: consent revoked or stale", user_id)
            return None

        embedding = await _get_embedding(content, api_key)
        if embedding is None:
            logging.warning("Skipping memory storage — embedding generation failed")
            return None

        return await _store_embedded_memory(
            user_id=user_id,
            db_text_content=db_text_content,
            embedding=embedding,
            source_type=source_type,
            metadata=metadata,
            ttl_days=ttl_days,
            expected_epoch=resolved_epoch,
            wing=wing,
            room=room,
            hall_type=hall_type,
        )


async def _store_embedded_memory(
    *,
    user_id: int,
    db_text_content: str,
    embedding: list[float],
    source_type: str,
    metadata: dict[str, Any] | None,
    ttl_days: int,
    expected_epoch: int,
    wing: str | None,
    room: str | None,
    hall_type: str | None,
) -> int | None:
    """Commit an already embedded memory under the exact consent generation."""
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days > 0 else None

    # Storage failures intentionally propagate so retry-aware background callers can
    # distinguish a transient database failure from a consent/validation no-op.
    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)

        await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
        consent_rows = await db_query(
            """
            SELECT ltm_enabled, memory_epoch, private_data_blocked
            FROM chats
            WHERE user_id = $1
            FOR UPDATE
            """,
            (user_id,),
            conn=conn,
        )

        # Missing chat state is never implicit consent. In particular, an old
        # retry must not survive account deletion and recreation.
        if not consent_rows:
            return None
        consent = consent_rows[0]
        current_epoch = int(consent["memory_epoch"] or 0)
        if (
            consent["ltm_enabled"] is not True
            or consent.get("private_data_blocked", False) is True
            or expected_epoch != current_epoch
        ):
            logging.debug(
                "Skipping stale memory storage for user %d: expected epoch %d, current epoch %d",
                user_id,
                expected_epoch,
                current_epoch,
            )
            return None

        cols = ["user_id", "content", "embedding", "source_type", "metadata", "expires_at"]
        vals = ["$1", "$2", "$3::halfvec", "$4", "$5::jsonb", "$6"]
        params: list = [
            user_id,
            db_text_content,
            f"[{','.join(str(v) for v in embedding)}]",
            source_type,
            metadata or {},
            expires_at,
        ]

        if wing or room or hall_type:
            idx = len(params) + 1
            cols.append("wing")
            vals.append(f"${idx}")
            params.append(wing)
            idx += 1
            cols.append("room")
            vals.append(f"${idx}")
            params.append(room)
            idx += 1
            cols.append("hall_type")
            vals.append(f"${idx}")
            params.append(hall_type)

        result = await db_query(
            f"INSERT INTO long_term_memory ({', '.join(cols)}) VALUES ({', '.join(vals)}) RETURNING id",
            tuple(params),
            conn=conn,
        )
        if not result:
            return None

        memory_id = int(result[0]["id"])
        await db_query(
            """
            DELETE FROM long_term_memory
            WHERE id IN (
                SELECT id
                FROM long_term_memory
                WHERE user_id = $1
                ORDER BY created_at DESC, id DESC
                OFFSET $2
            )
            RETURNING id
            """,
            (user_id, MAX_MEMORIES_PER_USER),
            conn=conn,
        )
        return memory_id


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


async def _is_ltm_read_enabled(user_id: int) -> bool:
    """Return durable read consent, failing closed on missing state or DB errors."""
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            rows = await db_query(
                """
                    SELECT ltm_enabled
                    FROM chats
                    WHERE user_id = $1
                    """,
                (user_id,),
                conn=conn,
            )
        return bool(rows and rows[0]["ltm_enabled"] is True)
    except Exception as e:
        logging.warning("LTM read consent check failed closed for user %d: %s", user_id, e)
        return False


async def _lock_ltm_read_consent(user_id: int, conn) -> bool:
    """Linearize tenant reads against a concurrent LTM opt-out.

    The caller must hold an explicit transaction. ``FOR SHARE`` makes a disable
    update wait until the protected LTM/graph read commits; if disable committed
    first, this sees ``false`` and the repository reads no private memory rows.
    """
    rows = await db_query(
        """
        SELECT ltm_enabled
        FROM chats
        WHERE user_id = $1
        FOR SHARE
        """,
        (user_id,),
        conn=conn,
    )
    return bool(rows and rows[0]["ltm_enabled"] is True)


async def search_memories(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 5,
    min_similarity: float = 0.5,
    _consent_checked: bool = False,
    expected_epoch: int | None = None,
) -> list[dict[str, Any]]:
    """Lease the external embedding/read phase for one exact generation."""
    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    if expected_epoch is None:
        expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="ltm:memory_search",
        require_ltm=True,
    ) as lease_current:
        if not lease_current:
            return []
        return await _search_memories_impl(
            user_id,
            query,
            api_key,
            limit=limit,
            min_similarity=min_similarity,
            _consent_checked=_consent_checked,
        )


async def _search_memories_impl(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 5,
    min_similarity: float = 0.5,
    _consent_checked: bool = False,
) -> list[dict[str, Any]]:
    """Search memories by semantic similarity with hybrid RRF + Adaptive Thresholding.

    Adaptive Thresholding (Change 2): instead of applying a hard cut-off, we fetch
    twice the requested limit with a relaxed floor (min_similarity - 0.12), then
    dynamically discard results that fall more than 15 percentage points below the
    best result's score. This prevents both "vector spam" (irrelevant facts that
    barely cross a fixed threshold) and "false negatives" (the one relevant fact
    that sits just below the threshold).

    Uses Reciprocal Rank Fusion to combine pgvector cosine similarity with
    pg_trgm keyword matching.  Falls back to pure semantic search if pg_trgm
    is not installed.

    Args:
        user_id: Owner of the memories.
        query: Search query text (already expanded if desired).
        api_key: Gemini API key for query embedding.
        limit: Maximum results after adaptive filtering.
        min_similarity: Soft floor — results below this are discarded even with
                        adaptive thresholding (protects against low-quality graphs).

    Returns:
        List of dicts with 'id', 'content', 'similarity', 'source_type', 'created_at'.
    """
    # This repository boundary is authoritative even when a caller bypasses the
    # handler guard. Consent must be known before sending text to an embedding API.
    if not _consent_checked and not await _is_ltm_read_enabled(user_id):
        return []

    query_embedding = await _get_embedding(query, api_key, task_type="RETRIEVAL_QUERY")
    if query_embedding is None:
        return []

    embedding_str = f"[{','.join(str(v) for v in query_embedding)}]"
    use_trgm = await _check_trgm_available()

    # Adaptive: relax the floor and fetch more candidates than needed
    adaptive_floor = max(0.40, min_similarity - 0.12)
    fetch_limit = limit * 2  # over-fetch; we'll trim with gap-filter below
    _adaptive_floor = adaptive_floor  # kept for gap-filter reference below

    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            if not await _lock_ltm_read_consent(user_id, conn):
                return []

            if use_trgm:
                # RRF hybrid: cosine similarity + keyword trigram
                results = await db_query(
                    """
                        WITH semantic AS (
                            SELECT id,
                                   ROW_NUMBER() OVER (ORDER BY embedding <=> $2::halfvec) AS rank_s
                            FROM long_term_memory
                            WHERE user_id = $1
                              AND (expires_at IS NULL OR expires_at > now())
                            ORDER BY embedding <=> $2::halfvec
                            LIMIT 40
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
                        ),
                        candidates AS (
                            SELECT COALESCE(s.id, k.id) AS id,
                                   s.rank_s,
                                   k.rank_k
                            FROM semantic s
                            FULL OUTER JOIN keyword k ON s.id = k.id
                        ),
                        scored AS (
                            SELECT memory.id,
                                   memory.content,
                                   memory.source_type,
                                   memory.metadata,
                                   memory.created_at,
                                   COALESCE(memory.rlhf_negative_count, 0) AS rlhf_neg,
                                   COALESCE(1 - (memory.embedding <=> $2::halfvec), 0.0) AS sim,
                                   candidates.rank_s,
                                   candidates.rank_k
                            FROM candidates
                            JOIN long_term_memory AS memory ON memory.id = candidates.id
                            WHERE memory.user_id = $1
                              AND (memory.expires_at IS NULL OR memory.expires_at > now())
                        ),
                        ranked AS (
                            SELECT scored.*,
                                   COALESCE(1.0 / (60 + rank_s), 0.0)
                                       + COALESCE(1.0 / (60 + rank_k), 0.0) AS rrf_score,
                                   GREATEST(0.0, sim - (rlhf_neg * 0.03)) AS adjusted_sim
                            FROM scored
                            WHERE sim >= $3 OR rank_k IS NOT NULL
                        )
                        SELECT id, content, source_type, metadata, created_at,
                               sim, rlhf_neg, rank_k, rrf_score,
                               rrf_score - (rlhf_neg * 0.003) AS final_score
                        FROM ranked
                        ORDER BY final_score DESC, adjusted_sim DESC, id DESC
                        LIMIT $4
                        """,
                    (user_id, embedding_str, adaptive_floor, fetch_limit, query[:500]),
                    conn=conn,
                )
            else:
                # Pure semantic fallback
                results = await db_query(
                    """
                        SELECT id, content, source_type, metadata, created_at,
                               COALESCE(rlhf_negative_count, 0) AS rlhf_neg,
                               1 - (embedding <=> $2::halfvec) AS sim,
                               GREATEST(
                                   0.0,
                                   1 - (embedding <=> $2::halfvec)
                                       - (COALESCE(rlhf_negative_count, 0) * 0.03)
                               ) AS adjusted_sim
                        FROM long_term_memory
                        WHERE user_id = $1
                          AND (expires_at IS NULL OR expires_at > now())
                          AND 1 - (embedding <=> $2::halfvec) >= $3
                        ORDER BY adjusted_sim DESC, id DESC
                        LIMIT $4
                        """,
                    (user_id, embedding_str, adaptive_floor, fetch_limit),
                    conn=conn,
                )

            if not results:
                return []

            # ─ Adaptive Gap Filtering ─────────────────────────────────
            # Keep only results within 15pp of the best score.
            # We intentionally do NOT re-apply min_similarity as a hard
            # floor here — that was already enforced by adaptive_floor in
            # the SQL WHERE clause. Using max(min_similarity, ...) was a
            # logic bug: it discarded valid results that passed the SQL gate
            # but fell just below the caller's soft threshold. The gap
            # filter's only job is to prune outliers *within* this candidate
            # set, not to act as a second hard threshold.
            rows = list(results)
            adjusted_similarities = [max(0.0, float(row["sim"]) - (int(row["rlhf_neg"] or 0) * 0.03)) for row in rows]
            top_sim = max(adjusted_similarities)
            gap_threshold = max(_adaptive_floor, top_sim - 0.08)

            filtered = []
            for r, adjusted_sim in zip(rows, adjusted_similarities, strict=True):
                is_keyword_match = r.get("rank_k") is not None
                if is_keyword_match or adjusted_sim >= gap_threshold:
                    filtered.append(
                        {
                            "id": r["id"],
                            "content": r["content"],
                            "similarity": adjusted_sim,
                            "source_type": r["source_type"],
                            "created_at": r["created_at"],
                        }
                    )
            filtered = filtered[:limit]

            if not filtered and rows:
                logging.debug(
                    "Memory search gap-filter dropped all %d candidates for user %d "
                    "(top_sim=%.3f, gap_threshold=%.3f, min_similarity=%.3f)",
                    len(rows),
                    user_id,
                    top_sim,
                    gap_threshold,
                    min_similarity,
                )

            return filtered

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
    expected_epoch: int | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    """Lease expansion, embeddings, graph traversal, and returned snapshot."""
    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    if expected_epoch is None:
        expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="ltm:graph_recall",
        require_ltm=True,
    ) as lease_current:
        if not lease_current:
            _current_retrieved_edge_ids.set((user_id, ()))
            return [], [], {}
        return await _search_memories_with_graph_impl(
            user_id,
            query,
            api_key,
            limit=limit,
            min_similarity=min_similarity,
        )


async def _search_memories_with_graph_impl(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 5,
    min_similarity: float = 0.5,
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    """Graph-augmented memory search with Multi-Query Expansion, 2-Hop traversal, and Temporal Context.

    Features:
    - Multi-Query Expansion: expands vague queries via Flash-Lite LLM before embedding.
    - 2-Hop Graph Traversal: follows outgoing edges from 1-hop neighbours.
    - Core No-Decay: edges with is_core=TRUE bypass time-decay.
    - Temporal Filtering: only current edges (valid_to IS NULL) are traversed.
    - Temporal Context: when superseded edges exist for the same entity pair,
      they are injected as <temporal_context> for LLM awareness.
    - RLHF Cache: retrieved edge IDs are cached for feedback penalization.
    - Edge Provenance: top-K edges surface passages through live normalized sources.

    Returns:
        (memories, graph_triples, source_passages) where source_passages maps
        triple strings to their originating LTM passage text.
    """
    # Clear task-local attribution before any await. An empty/failed
    # retrieval must never inherit edge IDs from an earlier response in this task.
    _current_retrieved_edge_ids.set((user_id, ()))

    # Fail closed before query expansion or embedding sends any user text to an
    # external model. Missing chat state is not implicit consent for recall.
    if not await _is_ltm_read_enabled(user_id):
        return [], [], {}

    # Multi-Query Expansion gate
    if _should_expand_query(query):
        expanded_query = await expand_query_with_llm(query, api_key)
    else:
        expanded_query = query

    # 1. Standard vector search for memories
    memories = await search_memories(
        user_id,
        expanded_query,
        api_key,
        limit=limit,
        min_similarity=min_similarity,
    )

    # 2. Graph traversal: find related entities
    graph_triples: list[str] = []
    source_passages: dict[str, str] = {}
    try:
        # Vector retrieval may have taken long enough for consent to change. Recheck
        # immediately before the separate graph embedding leaves the process.
        if not await _is_ltm_read_enabled(user_id):
            return [], [], {}

        query_embedding = await _get_embedding(expanded_query, api_key, task_type="RETRIEVAL_QUERY")
        if query_embedding is None:
            return memories, graph_triples, source_passages

        embedding_str = f"[{','.join(str(v) for v in query_embedding)}]"

        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            if not await _lock_ltm_read_consent(user_id, conn):
                return [], [], {}

            # Find top-K similar entity nodes
            nodes = await db_query(
                """
                    WITH live_node_sources AS (
                        SELECT source.*
                        FROM memory_node_sources AS source
                        JOIN long_term_memory AS memory
                          ON memory.id = source.memory_id
                         AND memory.user_id = source.user_id
                        WHERE source.user_id = $1
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    ), live_node_projection AS (
                        SELECT DISTINCT ON (source.node_id)
                               source.node_id, source.entity_type,
                               source.description, source.embedding
                        FROM live_node_sources AS source
                        WHERE source.attributes_complete IS TRUE
                        ORDER BY source.node_id,
                                 source.created_at DESC,
                                 source.memory_id DESC
                    )
                    SELECT node.id, node.entity_name,
                           COALESCE(projected.entity_type, 'concept') AS entity_type,
                           projected.description,
                           COALESCE(1 - (projected.embedding <=> $2::halfvec), 0.0) AS sim
                    FROM memory_nodes AS node
                    LEFT JOIN live_node_projection AS projected
                      ON projected.node_id = node.id
                    WHERE node.user_id = $1
                      AND EXISTS (
                          SELECT 1
                          FROM live_node_sources AS support
                          WHERE support.node_id = node.id
                      )
                    ORDER BY projected.embedding <=> $2::halfvec NULLS LAST,
                             node.id DESC
                    LIMIT 5
                    """,
                (user_id, embedding_str),
                conn=conn,
            )

            if not nodes:
                return memories, graph_triples, source_passages

            relevant_ids = [n["id"] for n in nodes if float(n["sim"]) >= 0.4]
            if not relevant_ids:
                return memories, graph_triples, source_passages

            # 2-hop traversal with temporal filtering (valid_to IS NULL)
            edges = await db_query(
                """
                    WITH live_edge_sources AS (
                        SELECT sources.*,
                               memory.created_at AS memory_created_at
                        FROM memory_edge_sources AS sources
                        JOIN long_term_memory AS memory
                          ON memory.id = sources.memory_id
                         AND memory.user_id = sources.user_id
                        WHERE sources.user_id = $1
                          AND memory.user_id = $1
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    ), edge_support AS (
                        SELECT source.edge_id,
                               array_agg(
                                   source.memory_id
                                   ORDER BY source.memory_created_at DESC,
                                            source.memory_id DESC
                               ) AS source_memory_ids,
                               MAX(source.created_at) AS latest_source_at
                        FROM live_edge_sources AS source
                        GROUP BY source.edge_id
                    ), complete_edge_aggregate AS (
                        SELECT source.edge_id,
                               MAX(source.weight) AS weight,
                               BOOL_OR(source.is_core) AS is_core
                        FROM live_edge_sources AS source
                        WHERE source.attributes_complete IS TRUE
                        GROUP BY source.edge_id
                    ), winning_predicate AS (
                        SELECT DISTINCT ON (source.edge_id)
                               source.edge_id, source.predicate
                        FROM live_edge_sources AS source
                        WHERE source.attributes_complete IS TRUE
                        ORDER BY source.edge_id,
                                 source.created_at DESC,
                                 source.memory_id DESC
                    ), projected_edges AS (
                        SELECT edge.id, edge.user_id,
                               edge.source_node, edge.target_node, edge.valid_to,
                               COALESCE(winner.predicate, 'RELATED_TO') AS predicate,
                               COALESCE(aggregate.weight, 0.0) AS weight,
                               COALESCE(aggregate.is_core, FALSE) AS is_core,
                               support.latest_source_at AS updated_at,
                               support.source_memory_ids
                        FROM memory_edges AS edge
                        JOIN edge_support AS support ON support.edge_id = edge.id
                        LEFT JOIN complete_edge_aggregate AS aggregate
                          ON aggregate.edge_id = edge.id
                        LEFT JOIN winning_predicate AS winner
                          ON winner.edge_id = edge.id
                        WHERE edge.user_id = $1
                    ),
                    hop1 AS (
                        SELECT
                            src.entity_name AS from_name,
                            e.predicate,
                            tgt.entity_name AS to_name,
                            tgt.id           AS tgt_id,
                            e.id             AS edge_id,
                            e.weight,
                            e.is_core,
                            e.updated_at,
                            e.source_memory_ids,
                            1                AS hop
                        FROM projected_edges e
                        JOIN memory_nodes src
                          ON e.source_node = src.id
                         AND src.user_id = e.user_id
                        JOIN memory_nodes tgt
                          ON e.target_node = tgt.id
                         AND tgt.user_id = e.user_id
                        WHERE e.user_id = $1
                          AND e.valid_to IS NULL
                          AND (
                              e.source_node = ANY($2::bigint[])
                           OR e.target_node = ANY($2::bigint[])
                          )
                    ),
                    hop2 AS (
                        SELECT
                            src2.entity_name AS from_name,
                            e2.predicate,
                            tgt2.entity_name AS to_name,
                            tgt2.id          AS tgt_id,
                            e2.id            AS edge_id,
                            e2.weight,
                            e2.is_core,
                            e2.updated_at,
                            e2.source_memory_ids,
                            2                AS hop
                        FROM hop1
                        JOIN projected_edges e2 ON e2.source_node = hop1.tgt_id
                        JOIN memory_nodes src2
                          ON e2.source_node = src2.id
                         AND src2.user_id = e2.user_id
                        JOIN memory_nodes tgt2
                          ON e2.target_node = tgt2.id
                         AND tgt2.user_id = e2.user_id
                        WHERE e2.user_id = $1
                          AND e2.valid_to IS NULL
                          AND NOT (e2.source_node = ANY($2::bigint[]))
                          AND NOT (e2.target_node = ANY($2::bigint[]))
                    ),
                    combined AS (
                        SELECT * FROM hop1
                        UNION ALL
                        SELECT * FROM hop2
                    ),
                    weighted AS (
                        SELECT combined.*,
                               CASE
                                   WHEN is_core THEN weight
                                   ELSE weight / (
                                       1.0 + EXTRACT(EPOCH FROM now() - COALESCE(updated_at, now()))
                                       / (86400.0 * 30)
                                   )
                               END AS effective_weight
                        FROM combined
                    ),
                    deduplicated AS (
                        SELECT DISTINCT ON (from_name, predicate, to_name)
                               from_name, predicate, to_name, weight, is_core, hop,
                               edge_id, source_memory_ids, updated_at, effective_weight
                        FROM weighted
                        ORDER BY from_name, predicate, to_name,
                                 effective_weight DESC,
                                 updated_at DESC NULLS LAST,
                                 hop ASC,
                                 edge_id DESC
                    )
                    SELECT
                        from_name, predicate, to_name, weight, is_core, hop, edge_id,
                        source_memory_ids, effective_weight
                    FROM deduplicated
                    ORDER BY effective_weight DESC,
                             updated_at DESC NULLS LAST,
                             edge_id DESC
                    LIMIT 15
                    """,
                (user_id, relevant_ids),
                conn=conn,
            )

            # Sort by effective_weight descending (core edges bubble up)
            edges_sorted = sorted(
                edges or [],
                key=lambda r: float(r["effective_weight"]),
                reverse=True,
            )

            # Cache edge IDs for RLHF feedback penalization
            retrieved_edge_ids = [int(e["edge_id"]) for e in edges_sorted if e.get("edge_id")]
            _current_retrieved_edge_ids.set((user_id, tuple(retrieved_edge_ids)))

            # ── Edge Provenance: batch-fetch source passages for top-K ──
            from app.repos.memory_config import SOURCE_PASSAGE_MAX_CHARS, SOURCE_PASSAGE_TOP_K

            # Collect the normalized live source IDs aggregated by the graph query.
            _top_edge_source_map: dict[str, list[int]] = {}
            for edge in edges_sorted[:SOURCE_PASSAGE_TOP_K]:
                _src_ids: list[int] = edge.get("source_memory_ids") or []
                if _src_ids:
                    triple_key = f"{edge['from_name']} — {edge['predicate']} → {edge['to_name']}"
                    _top_edge_source_map[triple_key] = _src_ids

            if _top_edge_source_map:
                all_source_ids = list(dict.fromkeys(sid for ids in _top_edge_source_map.values() for sid in ids))
                try:
                    source_rows = await db_query(
                        """
                            SELECT id, content FROM long_term_memory
                            WHERE user_id = $1
                              AND id = ANY($2::bigint[])
                              AND (expires_at IS NULL OR expires_at > now())
                            """,
                        (user_id, all_source_ids),
                        conn=conn,
                    )
                    _source_content_map = {
                        r["id"]: r["content"][:SOURCE_PASSAGE_MAX_CHARS] for r in (source_rows or [])
                    }
                    for triple_key, src_ids in _top_edge_source_map.items():
                        passages = [_source_content_map[sid] for sid in src_ids if sid in _source_content_map]
                        if passages:
                            source_passages[triple_key] = passages[0]  # best/first source
                except Exception as src_exc:
                    logging.debug("Source passage fetch failed (non-critical): %s", src_exc)

            for edge in edges_sorted:
                hop_label = " (indirect)" if edge.get("hop", 1) == 2 else ""
                core_label = " ★" if edge.get("is_core") else ""
                graph_triples.append(
                    f"{edge['from_name']} — {edge['predicate']} → {edge['to_name']}{core_label}{hop_label}"
                )

            # ── Temporal Context: find superseded edges for seed nodes ──
            superseded = await db_query(
                """
                    WITH live_sources AS (
                        SELECT source.*
                        FROM memory_edge_sources AS source
                        JOIN long_term_memory AS memory
                          ON memory.id = source.memory_id
                         AND memory.user_id = source.user_id
                        WHERE source.user_id = $1
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    ), live_support AS (
                        SELECT source.edge_id, MAX(source.created_at) AS latest_source_at
                        FROM live_sources AS source
                        GROUP BY source.edge_id
                    ), winning_predicate AS (
                        SELECT DISTINCT ON (source.edge_id)
                               source.edge_id, source.predicate
                        FROM live_sources AS source
                        WHERE source.attributes_complete IS TRUE
                        ORDER BY source.edge_id,
                                 source.created_at DESC,
                                 source.memory_id DESC
                    )
                    SELECT src.entity_name AS from_name,
                           COALESCE(winner.predicate, 'RELATED_TO') AS predicate,
                           tgt.entity_name AS to_name,
                           e.valid_from,
                           e.valid_to
                    FROM memory_edges e
                    JOIN live_support AS support ON support.edge_id = e.id
                    LEFT JOIN winning_predicate AS winner ON winner.edge_id = e.id
                    JOIN memory_nodes src
                      ON e.source_node = src.id
                     AND src.user_id = e.user_id
                    JOIN memory_nodes tgt
                      ON e.target_node = tgt.id
                     AND tgt.user_id = e.user_id
                    WHERE e.user_id = $1
                      AND e.valid_to IS NOT NULL
                      AND (
                          e.source_node = ANY($2::bigint[])
                       OR e.target_node = ANY($2::bigint[])
                      )
                    ORDER BY e.valid_to DESC
                    LIMIT 5
                    """,
                (user_id, relevant_ids),
                conn=conn,
            )

            if superseded:
                for old in superseded:
                    closed_date = str(old["valid_to"])[:10] if old.get("valid_to") else "?"
                    graph_triples.append(
                        f"[SUPERSEDED {closed_date}] {old['from_name']} — {old['predicate']} → {old['to_name']}"
                    )

            logging.debug(
                "Graph search for user %d: %d seed nodes, %d triples (incl. 2-hop + temporal), %d provenance, query: %r",
                user_id,
                len(relevant_ids),
                len(graph_triples),
                len(source_passages),
                expanded_query[:60],
            )
    except Exception as e:
        _current_retrieved_edge_ids.set((user_id, ()))
        logging.warning("Graph traversal failed (non-critical): %s", e)

    return memories, graph_triples, source_passages


async def search_memories_with_llm_judge(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 3,
    candidate_floor: float = 0.42,
    expected_epoch: int | None = None,
) -> list[dict[str, Any]]:
    """Lease candidate recall and the provider judge as one private use."""
    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    if expected_epoch is None:
        expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="ltm:memory_judge",
        require_ltm=True,
    ) as lease_current:
        if not lease_current:
            return []
        return await _search_memories_with_llm_judge_impl(
            user_id,
            query,
            api_key,
            limit=limit,
            candidate_floor=candidate_floor,
        )


async def _search_memories_with_llm_judge_impl(
    user_id: int,
    query: str,
    api_key: str,
    *,
    limit: int = 3,
    candidate_floor: float = 0.42,
) -> list[dict[str, Any]]:
    """Low-confidence fallback: fetch candidates + LLM relevance judge.

    Called when primary vector search (min_similarity=0.60) returns nothing.
    Strategy (RF-Mem "recollection path", 2025):
      1. Retrieve top-k candidates at a lower floor (default 0.42) — broad net.
      2. One cheap Flash-Lite call judges each candidate's relevance to the query.
      3. Only genuinely relevant ones are returned (tagged llm_judged=True).

    This avoids discarding valid memories that scored just below the primary
    threshold due to multimodal embedding space compression (gemini-embedding-2-preview)
    or semantically diluted phrasing (e.g., "Мою жену зовут X. А твою?").

    Returns at most `limit` memories tagged with {'llm_judged': True}.
    Returns [] silently on any error — this is strictly non-blocking.
    """

    # Step 1: Over-fetch low-confidence candidates
    candidates = await search_memories(
        user_id,
        query,
        api_key,
        limit=limit * 2,
        min_similarity=candidate_floor,
    )
    if not candidates:
        logging.debug(
            "LLM judge fallback: no candidates even at floor=%.2f for user %d",
            candidate_floor,
            user_id,
        )
        return []

    # Retrieval and its embedding are a separate phase. A user can opt out while
    # candidates are being fetched, so never send stored facts to the judge without
    # a fresh durable check immediately before this external call.
    if not await _is_ltm_read_enabled(user_id):
        return []

    # Step 2: Build batch judge prompt — one call for all candidates
    facts_lines = "\n".join(f"{i}. {c['content'][:300]}" for i, c in enumerate(candidates))
    prompt = (
        "You are a relevance judge for a personal memory assistant.\n"
        f'User message: "{query[:400]}"\n\n'
        "These are stored personal facts. Decide which facts are clearly relevant "
        "to answering the user's message (even if indirectly).\n"
        "Output ONLY valid JSON array: "
        '[{"index": 0, "relevant": true}, {"index": 1, "relevant": false}, ...]\n\n'
        f"Facts:\n{facts_lines}"
    )

    try:
        client = get_cached_genai_client(api_key)
        resp = await client.aio.models.generate_content(
            model=QUERY_EXPANSION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=256,
                response_mime_type="application/json",
            ),
        )
        judgements: list[dict[str, Any]] = json.loads(resp.text or "[]")
        relevant_indices = {int(j["index"]) for j in judgements if j.get("relevant")}

        result = [{**c, "llm_judged": True} for i, c in enumerate(candidates) if i in relevant_indices][:limit]

        logging.info(
            "LLM judge fallback: %d/%d candidates relevant for user %d (query=%r)",
            len(result),
            len(candidates),
            user_id,
            query[:60],
        )
        return result

    except Exception as exc:
        logging.debug("LLM judge fallback skipped (non-critical): %s", exc)
        return []


def get_current_retrieved_edge_ids(user_id: int) -> list[int]:
    """Return edge IDs from the current task's latest retrieval for this user."""
    current = _current_retrieved_edge_ids.get()
    if current is None or current[0] != user_id:
        return []
    return list(current[1])


def bind_retrieved_edges_to_response(
    user_id: int,
    response_message_id: int,
    *,
    edge_ids: Iterable[int] | None = None,
) -> list[int]:
    """Bind this task's graph attribution to an exact response message.

    An empty or cross-user context removes any prior value for the response key,
    preventing message-ID reuse or an empty retrieval from retaining stale edges.
    ``edge_ids`` supports handoff from a child task (for example ``asyncio.gather``);
    when omitted, the current task-local retrieval is used.
    """
    key = (user_id, response_message_id)
    selected_edge_ids = (
        tuple(dict.fromkeys(int(edge_id) for edge_id in edge_ids))
        if edge_ids is not None
        else tuple(get_current_retrieved_edge_ids(user_id))
    )

    if selected_edge_ids:
        _response_retrieved_edge_ids[key] = selected_edge_ids
    else:
        _response_retrieved_edge_ids.pop(key, None)

    return list(selected_edge_ids)


def get_response_retrieved_edge_ids(user_id: int, response_message_id: int) -> list[int]:
    """Return graph edge IDs attributed to one user-owned response message."""
    return list(_response_retrieved_edge_ids.get((user_id, response_message_id), ()))


async def penalize_graph_edges(
    user_id: int,
    edge_ids: list[int] | None = None,
    *,
    penalty: float = 0.10,
) -> int:
    """Reduce weight of specified graph edges as RLHF negative feedback.

    Attribution must be supplied explicitly for the exact response. ``None`` is a
    strict no-op; it must never fall back to a stale process-global retrieval.
    Weight is clamped to a minimum of 0.05 to prevent permanent deletion.

    Returns:
        Number of edges penalized.
    """
    if not edge_ids:
        return 0

    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            result = await conn.execute(
                """
                    UPDATE memory_edges
                    SET weight = GREATEST(0.05, weight - $1),
                        updated_at = now()
                    WHERE id = ANY($2::bigint[])
                      AND user_id = $3
                      AND valid_to IS NULL
                    """,
                penalty,
                edge_ids,
                user_id,
            )
            # asyncpg execute returns a status string like "UPDATE 3"
            count = int(result.split()[-1]) if result else 0

            # ── Provenance RLHF: cascade penalty to source memories ──
            if count > 0:
                try:
                    source_rows = await db_query(
                        """
                            SELECT DISTINCT sources.memory_id AS mem_id
                            FROM memory_edge_sources AS sources
                            JOIN long_term_memory AS memory
                              ON memory.id = sources.memory_id
                             AND memory.user_id = sources.user_id
                            WHERE sources.edge_id = ANY($1::bigint[])
                              AND sources.user_id = $2
                              AND memory.user_id = $2
                              AND (memory.expires_at IS NULL OR memory.expires_at > now())
                            """,
                        (edge_ids, user_id),
                        conn=conn,
                    )
                    if source_rows:
                        source_mem_ids = [r["mem_id"] for r in source_rows]
                        await conn.execute(
                            """
                                UPDATE long_term_memory
                                SET rlhf_negative_count = COALESCE(rlhf_negative_count, 0) + 1
                                WHERE id = ANY($1::bigint[])
                                  AND user_id = $2
                                """,
                            source_mem_ids,
                            user_id,
                        )
                        logging.info(
                            "RLHF: cascaded penalty to %d source memories for user %d",
                            len(source_mem_ids),
                            user_id,
                        )
                except Exception as cascade_exc:
                    logging.debug("RLHF source cascade failed (non-critical): %s", cascade_exc)

            if count > 0:
                logging.info(
                    "RLHF: penalized %d graph edges (penalty=%.2f) for user %d",
                    count,
                    penalty,
                    user_id,
                )
            return count
    except Exception as e:
        logging.warning("RLHF edge penalization failed for user %d: %s", user_id, e)
        return 0


async def delete_user_memories(user_id: int) -> int:
    """Delete all memories + graph data, propagating failures to the caller."""
    # Cancel local captures before taking the durable lock. The epoch bump below is
    # still authoritative for work running in another process or already past cancel.
    try:
        from app.repos.memory_autosave import cancel_user_memory_tasks

        await cancel_user_memory_tasks(user_id)
    except Exception as e:
        logging.warning("Failed to cancel local memory tasks for user %d: %s", user_id, e)

    from app.repos.memory_consent import private_data_barrier

    async with private_data_barrier(
        user_id,
        is_admin=False,
        ltm_only=True,
    ) as (privacy_barrier, previous_ltm_enabled):
        from app.voice_engine import get_voice_reply_manager

        await get_voice_reply_manager().purge_user_jobs(user_id, ltm_only=True)

        # Phase 2 verifies the barrier, deletes atomically, then unblocks with a
        # new generation so work queued while blocked cannot become current.
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            barrier_rows = await db_query(
                """
                SELECT user_id FROM chats
                WHERE user_id = $1
                  AND memory_epoch = $2
                  AND private_data_blocked IS TRUE
                FOR UPDATE
                """,
                (user_id, privacy_barrier),
                conn=conn,
            )
            if not barrier_rows:
                raise RuntimeError("memory deletion privacy barrier is no longer current")

            await conn.execute("DELETE FROM memory_edges WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM memory_nodes WHERE user_id = $1", user_id)
            result = await db_query(
                "DELETE FROM long_term_memory WHERE user_id = $1 RETURNING id",
                (user_id,),
                conn=conn,
            )
            count = len(result) if result else 0
            restored_rows = await db_query(
                """
                UPDATE chats
                SET ltm_enabled = $3,
                    private_data_blocked = FALSE,
                    memory_epoch = nextval('memory_consent_epoch_seq')
                WHERE user_id = $1 AND memory_epoch = $2
                RETURNING memory_epoch
                """,
                (user_id, privacy_barrier, previous_ltm_enabled),
                conn=conn,
            )
            if not restored_rows:
                raise RuntimeError("memory deletion failed to release privacy barrier")

    for attribution_key in list(_response_retrieved_edge_ids):
        if attribution_key[0] == user_id:
            _response_retrieved_edge_ids.pop(attribution_key, None)
    logging.info("Deleted %d memories + graph data for user %d", count, user_id)
    return count


async def delete_memory(user_id: int, memory_id: int) -> bool:
    """Delete a single memory by ID (RLS-safe: scoped to user_id)."""
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            # Provenance rows cascade from the deleted memory; migration 067's
            # deferred trigger removes orphan edges and graph nodes at commit.
            result = await db_query(
                "DELETE FROM long_term_memory WHERE id = $1 AND user_id = $2 RETURNING id",
                (memory_id, user_id),
                conn=conn,
            )
            return bool(result)
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
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
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
    except Exception as e:
        logging.error("Failed to list memories for user %d: %s", user_id, e, exc_info=True)
        return []


async def cleanup_expired_memories() -> int:
    """Delete all expired memories across all users. Returns count deleted."""
    # Discover work in a short cross-tenant transaction. Database failures must
    # propagate so the scheduler cannot report a misleading successful zero.
    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(0, True, conn=conn)
        await db_query(
            "DELETE FROM private_data_leases WHERE expires_at <= now()",
            (),
            conn=conn,
        )
        maintenance_rows = await db_query(
            """
            SELECT DISTINCT candidate.user_id
            FROM (
                SELECT user_id
                FROM long_term_memory
                WHERE expires_at IS NOT NULL AND expires_at < now()
                UNION ALL
                SELECT edge.user_id
                FROM memory_edges AS edge
                WHERE COALESCE(edge.updated_at, edge.created_at) < now() - INTERVAL '1 hour'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memory_edge_sources AS source
                      WHERE source.edge_id = edge.id
                        AND source.user_id = edge.user_id
                  )
                UNION ALL
                SELECT node.user_id
                FROM memory_nodes AS node
                WHERE node.updated_at < now() - INTERVAL '1 hour'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM memory_edges AS edge
                      WHERE edge.user_id = node.user_id
                        AND (edge.source_node = node.id OR edge.target_node = node.id)
                  )
            ) AS candidate
            ORDER BY candidate.user_id
            """,
            (),
            conn=conn,
        )

    maintenance_user_ids = sorted({int(row["user_id"]) for row in (maintenance_rows or [])})
    count = 0
    failures: list[tuple[int, Exception]] = []

    # Isolate every tenant in its own short transaction and advisory lock. A
    # single tenant failure cannot roll back already-completed maintenance or
    # prevent later tenants from being attempted.
    for maintenance_user_id in maintenance_user_ids:
        try:
            async with db_manager.pool.acquire() as conn, conn.transaction():
                await set_user_context(0, True, conn=conn)
                await conn.execute("SELECT pg_advisory_xact_lock($1)", maintenance_user_id)
                result = await db_query(
                    """
                    DELETE FROM long_term_memory
                    WHERE user_id = $1
                      AND expires_at IS NOT NULL
                      AND expires_at < now()
                    RETURNING id
                    """,
                    (maintenance_user_id,),
                    conn=conn,
                )
                await db_query(
                    "SELECT delete_stale_orphaned_memory_nodes($1)",
                    (maintenance_user_id,),
                    conn=conn,
                )
            count += len(result) if result else 0
        except Exception as e:
            failures.append((maintenance_user_id, e))
            logging.error(
                "Memory cleanup failed for user %d: %s",
                maintenance_user_id,
                e,
                exc_info=True,
            )

    if failures:
        failed_ids = ", ".join(str(user_id) for user_id, _error in failures)
        raise RuntimeError(f"Memory cleanup incomplete for {len(failures)} user(s): {failed_ids}") from failures[0][1]

    if count > 0:
        logging.info("Cleaned up %d expired memories", count)
    return count


async def export_user_memory(user_id: int) -> dict[str, list[dict[str, Any]]]:
    """Export a tenant's LTM and graph metadata without vector embeddings.

    Database failures propagate to the caller so privacy export handlers can report
    a failed/incomplete export instead of returning a misleading partial archive.
    """
    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        memories = await db_query(
            """
            SELECT id, user_id, content, source_type, metadata,
                   created_at, expires_at, consolidated_at,
                   wing, room, hall_type, rlhf_negative_count
            FROM long_term_memory
            WHERE user_id = $1
            ORDER BY created_at ASC, id ASC
            """,
            (user_id,),
            conn=conn,
        )
        nodes = await db_query(
            """
            SELECT id, user_id, entity_name, entity_type, description,
                   file_id, file_type, chat_id, actor_user_id,
                   wing, room, created_at, updated_at
            FROM memory_nodes
            WHERE user_id = $1
            ORDER BY id ASC
            """,
            (user_id,),
            conn=conn,
        )
        edges = await db_query(
            """
            SELECT id, user_id, source_node, target_node, predicate,
                   weight, is_core, valid_from, valid_to,
                   chat_id, actor_user_id, is_public,
                   created_at, updated_at
            FROM memory_edges
            WHERE user_id = $1
            ORDER BY id ASC
            """,
            (user_id,),
            conn=conn,
        )
        edge_sources = await db_query(
            """
            SELECT edge_id, memory_id, user_id, predicate,
                   weight, is_core, attributes_complete, created_at
            FROM memory_edge_sources
            WHERE user_id = $1
            ORDER BY edge_id ASC, memory_id ASC
            """,
            (user_id,),
            conn=conn,
        )
        node_sources = await db_query(
            """
            SELECT node_id, memory_id, user_id, entity_type, description,
                   wing, room, file_id, file_type, attributes_complete, created_at
            FROM memory_node_sources
            WHERE user_id = $1
            ORDER BY node_id ASC, memory_id ASC
            """,
            (user_id,),
            conn=conn,
        )
        derivation_sources = await db_query(
            """
            SELECT derived_memory_id, source_memory_id, user_id, created_at
            FROM memory_derivation_sources
            WHERE user_id = $1
            ORDER BY derived_memory_id ASC, source_memory_id ASC
            """,
            (user_id,),
            conn=conn,
        )

        return {
            "memories": [dict(row) for row in (memories or [])],
            "nodes": [dict(row) for row in (nodes or [])],
            "edges": [dict(row) for row in (edges or [])],
            "edge_sources": [dict(row) for row in (edge_sources or [])],
            "node_sources": [dict(row) for row in (node_sources or [])],
            "derivation_sources": [dict(row) for row in (derivation_sources or [])],
        }


async def get_memory_stats(user_id: int) -> dict[str, Any]:
    """Get memory usage stats for a user."""
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
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
    except Exception as e:
        logging.error("Memory stats failed for user %d: %s", user_id, e, exc_info=True)

    return {"total": 0, "limit": MAX_MEMORIES_PER_USER}
