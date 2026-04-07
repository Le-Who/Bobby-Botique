# /app/repos/memory.py
"""Long-term memory repository — semantic search over past conversations.

Uses pgvector for embedding storage and HNSW-indexed cosine similarity search.
Embeddings are generated via Gemini's embedding API (gemini-embedding-2-preview, 768-dim halfvec).
"""

import logging
import re
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

# ── Constants (re-exported from memory_config for backward compatibility) ─────
from app.repos.memory_config import (
    DEFAULT_MEMORY_TTL_DAYS,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    MAX_MEMORIES_PER_USER,
    QUERY_EXPANSION_MODEL,
)

__all__ = [
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
    "QUERY_EXPANSION_MODEL",
    "MAX_MEMORIES_PER_USER",
    "DEFAULT_MEMORY_TTL_DAYS",
]

# Cached flag: True if pg_trgm extension is available in this database
_trgm_available: bool | None = None

# RLHF: maps user_id → list of memory_edges.id from the most recent graph retrieval.
# Used by penalize_graph_edges() when a user taps 👎 on a response.
_last_retrieved_edge_ids: dict[int, list[Any]] = {}


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

                # Build column list dynamically — taxonomy columns are optional
                # until migration 032 runs on the target database.
                cols = ["user_id", "content", "embedding", "source_type", "metadata", "expires_at"]
                vals = ["$1", "$2", "$3::halfvec", "$4", "$5::jsonb", "$6"]
                params: list = [
                    user_id,
                    db_text_content,
                    f"[{','.join(str(v) for v in embedding)}]",
                    source_type,
                    __import__("json").dumps(metadata or {}),
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
                    f"INSERT INTO long_term_memory ({', '.join(cols)}) "
                    f"VALUES ({', '.join(vals)}) RETURNING id",
                    tuple(params),
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
                        (user_id, embedding_str, adaptive_floor, fetch_limit, query[:500]),
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
                        (user_id, embedding_str, adaptive_floor, fetch_limit),
                        conn=conn,
                    )

                if not results:
                    return []

                # ─ Adaptive Gap Filtering ─────────────────────────────────
                # Keep only results within 15pp of the best score.
                # We intentionally do NOT re-apply min_similarity as a hard
                # floor here — that was already enforced by adaptive_floor in
                # the SQL WHERE clause.  Using max(min_similarity, ...) was a
                # logic bug: it discarded valid results that passed the SQL gate
                # but fell just below the caller's soft threshold.  The gap
                # filter's only job is to prune outliers *within* this candidate
                # set, not to act as a second hard threshold.
                rows = list(results)
                top_sim = float(rows[0].get("sim", rows[0].get("similarity", 0)))
                gap_threshold = max(_adaptive_floor, top_sim - 0.15)

                filtered = [
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "similarity": float(r.get("sim", r.get("similarity", 0))),
                        "source_type": r["source_type"],
                        "created_at": r["created_at"],
                    }
                    for r in rows
                    if float(r.get("sim", r.get("similarity", 0))) >= gap_threshold
                ][:limit]

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
    """Graph-augmented memory search with Multi-Query Expansion, 2-Hop traversal, and Temporal Context.

    Features:
    - Multi-Query Expansion: expands vague queries via Flash-Lite LLM before embedding.
    - 2-Hop Graph Traversal: follows outgoing edges from 1-hop neighbours.
    - Core No-Decay: edges with is_core=TRUE bypass time-decay.
    - Temporal Filtering: only current edges (valid_to IS NULL) are traversed.
    - Temporal Context: when superseded edges exist for the same entity pair,
      they are injected as <temporal_context> for LLM awareness.
    - RLHF Cache: retrieved edge IDs are cached for feedback penalization.
    """
    # Multi-Query Expansion gate
    if _should_expand_query(query):
        expanded_query = await expand_query_with_llm(query, api_key)
    else:
        expanded_query = query

    # 1. Standard vector search for memories
    memories = await search_memories(user_id, expanded_query, api_key, limit=limit, min_similarity=min_similarity)

    # 2. Graph traversal: find related entities
    graph_triples: list[str] = []
    try:
        query_embedding = await _get_embedding(expanded_query, api_key, task_type="RETRIEVAL_QUERY")
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

                relevant_ids = [n["id"] for n in nodes if float(n.get("sim", 0)) >= 0.4]
                if not relevant_ids:
                    return memories, graph_triples

                # 2-hop traversal with temporal filtering (valid_to IS NULL)
                edges = await db_query(
                    """
                    WITH hop1 AS (
                        SELECT
                            src.entity_name AS from_name,
                            e.predicate,
                            tgt.entity_name AS to_name,
                            tgt.id           AS tgt_id,
                            e.id             AS edge_id,
                            e.weight,
                            e.is_core,
                            e.updated_at,
                            1                AS hop
                        FROM memory_edges e
                        JOIN memory_nodes src ON e.source_node = src.id
                        JOIN memory_nodes tgt ON e.target_node = tgt.id
                        WHERE e.user_id = $1
                          AND e.valid_to IS NULL
                          AND (
                              e.source_node = ANY($2::uuid[])
                           OR e.target_node = ANY($2::uuid[])
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
                            2                AS hop
                        FROM hop1
                        JOIN memory_edges e2 ON e2.source_node = hop1.tgt_id
                        JOIN memory_nodes src2 ON e2.source_node = src2.id
                        JOIN memory_nodes tgt2 ON e2.target_node = tgt2.id
                        WHERE e2.user_id = $1
                          AND e2.valid_to IS NULL
                          AND NOT (e2.source_node = ANY($2::uuid[]))
                          AND NOT (e2.target_node = ANY($2::uuid[]))
                    ),
                    combined AS (
                        SELECT * FROM hop1
                        UNION ALL
                        SELECT * FROM hop2
                    )
                    SELECT DISTINCT ON (from_name, predicate, to_name)
                        from_name, predicate, to_name, weight, is_core, hop, edge_id,
                        CASE
                            WHEN is_core THEN weight
                            ELSE weight / (
                                1.0 + EXTRACT(EPOCH FROM now() - COALESCE(updated_at, now()))
                                / (86400.0 * 30))
                        END AS effective_weight
                    FROM combined
                    ORDER BY from_name, predicate, to_name, hop ASC
                    LIMIT 15
                    """,
                    (user_id, relevant_ids),
                    conn=conn,
                )

                # Sort by effective_weight descending (core edges bubble up)
                edges_sorted = sorted(
                    edges or [],
                    key=lambda r: float(r.get("effective_weight", 0)),
                    reverse=True,
                )

                # Cache edge IDs for RLHF feedback penalization
                retrieved_edge_ids = [e["edge_id"] for e in edges_sorted if e.get("edge_id")]
                _last_retrieved_edge_ids[user_id] = retrieved_edge_ids

                for edge in edges_sorted:
                    hop_label = " (indirect)" if edge.get("hop", 1) == 2 else ""
                    core_label = " ★" if edge.get("is_core") else ""
                    graph_triples.append(
                        f"{edge['from_name']} — {edge['predicate']} → {edge['to_name']}{core_label}{hop_label}"
                    )

                # ── Temporal Context: find superseded edges for seed nodes ──
                superseded = await db_query(
                    """
                    SELECT src.entity_name AS from_name,
                           e.predicate,
                           tgt.entity_name AS to_name,
                           e.valid_from,
                           e.valid_to
                    FROM memory_edges e
                    JOIN memory_nodes src ON e.source_node = src.id
                    JOIN memory_nodes tgt ON e.target_node = tgt.id
                    WHERE e.user_id = $1
                      AND e.valid_to IS NOT NULL
                      AND (
                          e.source_node = ANY($2::uuid[])
                       OR e.target_node = ANY($2::uuid[])
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
                    "Graph search for user %d: %d seed nodes, %d triples (incl. 2-hop + temporal), query: %r",
                    user_id,
                    len(relevant_ids),
                    len(graph_triples),
                    expanded_query[:60],
                )
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.warning("Graph traversal failed (non-critical): %s", e)

    return memories, graph_triples


async def search_memories_with_llm_judge(
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
    import json

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


def get_last_retrieved_edge_ids(user_id: int) -> list[Any]:
    """Return the cached edge IDs from the most recent graph retrieval for a user.

    This is used by the RLHF feedback loop: when the user taps 👎 on a response,
    the edges that contributed to that response are penalized.
    """
    return _last_retrieved_edge_ids.get(user_id, [])


async def penalize_graph_edges(
    user_id: int,
    edge_ids: list[Any] | None = None,
    *,
    penalty: float = 0.10,
) -> int:
    """Reduce weight of specified graph edges as RLHF negative feedback.

    If edge_ids is None, uses the cached IDs from the most recent retrieval.
    Weight is clamped to a minimum of 0.05 to prevent permanent deletion.

    Returns:
        Number of edges penalized.
    """
    if edge_ids is None:
        edge_ids = get_last_retrieved_edge_ids(user_id)

    if not edge_ids:
        return 0

    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                result = await conn.execute(
                    """
                    UPDATE memory_edges
                    SET weight = GREATEST(0.05, weight - $1),
                        updated_at = now()
                    WHERE id = ANY($2::uuid[])
                      AND user_id = $3
                      AND valid_to IS NULL
                    """,
                    penalty,
                    edge_ids,
                    user_id,
                )
                # asyncpg execute returns a status string like "UPDATE 3"
                count = int(result.split()[-1]) if result else 0
                if count > 0:
                    logging.info(
                        "RLHF: penalized %d graph edges (penalty=%.2f) for user %d",
                        count,
                        penalty,
                        user_id,
                    )
                return count
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.warning("RLHF edge penalization failed for user %d: %s", user_id, e)
        return 0


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
