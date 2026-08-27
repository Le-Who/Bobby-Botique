# /app/repos/memory_consolidation.py
"""Just-In-Time Memory Consolidation (Change 5).

Triggers consolidation when raw memories exceed a token threshold
(~8000 tokens) OR a temporal threshold (7+ days since last consolidation).
Uses a cheap LLM call to extract atomic "Persona Facts" from raw memories,
then replaces the batch with the consolidated facts.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.database import db_manager
from app.repos.db_helpers import db_query, set_user_context
from app.repos.memory_config import (
    CHARS_PER_TOKEN as _CHARS_PER_TOKEN,
)
from app.repos.memory_config import (
    CONSOLIDATION_MODEL as _CONSOLIDATION_MODEL,
)
from app.repos.memory_config import (
    CONSOLIDATION_TEMPORAL_DAYS as TEMPORAL_THRESHOLD_DAYS,
)
from app.repos.memory_config import (
    CONSOLIDATION_TOKEN_THRESHOLD as TOKEN_THRESHOLD,
)
from app.repos.memory_config import (
    MAX_PERSONA_FACTS,
    MIN_PERSONA_FACTS,
)
from app.repos.memory_graph_writer import (
    GraphEdgeCandidate,
    GraphMutationPlan,
    GraphNodeCandidate,
    write_graph,
)
from app.utils.json_compat import json

# ── Debounce gate constants ─────────────────────────────────────────────
_MSG_GATE = 20  # check should_consolidate every Nth message
_TIME_GATE = 900.0  # or every 15 minutes (seconds)
_consolidation_state: dict[int, dict] = {}  # {user_id: {"msg_count": int, "last_check_ts": float}}


def should_check_consolidation(user_id: int) -> bool:
    """O(1) in-memory gate — returns True only when it's time to call should_consolidate().

    Prevents firing a DB SELECT + potential LLM call on every single message.
    Triggers when:
    - msg_count >= _MSG_GATE (every 20th message), OR
    - time since last check >= _TIME_GATE (every 15 minutes)
    """
    now = time.monotonic()
    state = _consolidation_state.get(user_id)

    if state is None:
        _consolidation_state[user_id] = {"msg_count": 1, "last_check_ts": now}
        return False

    state["msg_count"] += 1

    # Message count gate
    if state["msg_count"] >= _MSG_GATE:
        state["msg_count"] = 0
        state["last_check_ts"] = now
        return True

    # Time gate
    if (now - state["last_check_ts"]) >= _TIME_GATE:
        state["msg_count"] = 0
        state["last_check_ts"] = now
        return True

    return False


def reset_consolidation_state(user_id: int | None = None) -> None:
    """Reset debounce state for a user (or all users). Useful for testing."""
    if user_id is None:
        _consolidation_state.clear()
    else:
        _consolidation_state.pop(user_id, None)


def _estimate_tokens(text: str) -> int:
    """Fast token estimate for Cyrillic/Latin mixed text."""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


async def get_raw_memories_for_consolidation(
    user_id: int,
    *,
    expected_epoch: int | None = None,
) -> list[dict[str, Any]]:
    """Return consent-valid, non-consolidated memories with token estimates.

    Derived/background work fails closed when the user has no ``chats`` row;
    unlike the raw-memory write path, it does not infer epoch-zero consent.
    """
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            results = await db_query(
                """
                    SELECT m.id, m.content, m.source_type, m.created_at, m.expires_at,
                           c.memory_epoch
                    FROM long_term_memory m
                    JOIN chats c ON c.user_id = m.user_id
                                AND c.ltm_enabled IS TRUE
                    WHERE m.user_id = $1
                      AND ($2::bigint IS NULL OR c.memory_epoch = $2)
                      AND m.source_type != 'consolidated'
                      AND m.consolidated_at IS NULL
                      AND (m.expires_at IS NULL OR m.expires_at > now())
                    ORDER BY m.created_at ASC, m.id ASC
                    """,
                (user_id, expected_epoch),
                conn=conn,
            )
            return [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "source_type": r["source_type"],
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "memory_epoch": r["memory_epoch"],
                    "est_tokens": _estimate_tokens(r["content"]),
                }
                for r in (results or [])
            ]
    except Exception as e:
        logging.error("Failed to get raw memories for user %d: %s", user_id, e, exc_info=True)
        return []


async def get_last_consolidation_time(user_id: int) -> datetime | None:
    """Return the timestamp of the newest consolidated memory, or None."""
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            results = await db_query(
                """
                    SELECT MAX(created_at) AS last_ts
                    FROM long_term_memory
                    WHERE user_id = $1 AND source_type = 'consolidated'
                    """,
                (user_id,),
                conn=conn,
            )
            if results and results[0]["last_ts"]:
                return results[0]["last_ts"]
            return None
    except Exception as e:
        logging.error("Failed to get last consolidation time for user %d: %s", user_id, e, exc_info=True)
        return None


async def should_consolidate(user_id: int) -> bool:
    """Check if consolidation should trigger (token OR temporal threshold)."""
    raw_memories = await get_raw_memories_for_consolidation(user_id)
    if not raw_memories:
        return False

    # Token threshold
    total_tokens = sum(m["est_tokens"] for m in raw_memories)
    if total_tokens >= TOKEN_THRESHOLD:
        logging.info(
            "Consolidation triggered for user %d: %d tokens >= %d threshold",
            user_id,
            total_tokens,
            TOKEN_THRESHOLD,
        )
        return True

    # Temporal threshold
    last_ts = await get_last_consolidation_time(user_id)
    now = datetime.now(UTC)
    if last_ts is None:
        # Never consolidated — check if oldest memory is old enough
        oldest = raw_memories[0]["created_at"]
        if oldest and (now - oldest) > timedelta(days=TEMPORAL_THRESHOLD_DAYS):
            logging.info(
                "Consolidation triggered for user %d: oldest memory is %s days old",
                user_id,
                (now - oldest).days,
            )
            return True
    elif (now - last_ts) > timedelta(days=TEMPORAL_THRESHOLD_DAYS):
        logging.info(
            "Consolidation triggered for user %d: %s days since last consolidation",
            user_id,
            (now - last_ts).days,
        )
        return True

    return False


async def maybe_consolidate(
    user_id: int,
    api_key: str,
    *,
    expected_epoch: int | None = None,
) -> int:
    """Check threshold and consolidate in a single DB round-trip if needed.

    ⚡ Bolt Optimization: replaces the two-call pattern:
        if await should_consolidate(uid): await consolidate_memories(uid, key)
    with a single function that fetches raw_memories ONCE, checks thresholds
    against the already-fetched data, and passes it directly to consolidation
    -- eliminating one guaranteed DB SELECT + row deserialization on every
    triggered consolidation.

    Performance: saves ~5-20ms per consolidation trigger (1 fewer DB round-trip).
    Returns number of new persona facts created, or 0 if no consolidation needed.
    """
    if expected_epoch is None:
        from app.repos.memory_consent import resolve_current_epoch

        expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
        if expected_epoch is None:
            return 0
    raw_memories = await get_raw_memories_for_consolidation(user_id, expected_epoch=expected_epoch)
    if not raw_memories:
        return 0

    # Check token threshold against the already-fetched rows
    total_tokens = sum(m["est_tokens"] for m in raw_memories)
    triggered = False

    if total_tokens >= TOKEN_THRESHOLD:
        logging.info(
            "Consolidation triggered for user %d: %d tokens >= %d threshold",
            user_id,
            total_tokens,
            TOKEN_THRESHOLD,
        )
        triggered = True
    else:
        # Temporal threshold: one extra DB query, but cheaper than the second
        # full re-fetch that consolidate_memories would have done otherwise.
        last_ts = await get_last_consolidation_time(user_id)
        now = datetime.now(UTC)
        if last_ts is None:
            oldest = raw_memories[0]["created_at"]
            if oldest and (now - oldest) > timedelta(days=TEMPORAL_THRESHOLD_DAYS):
                logging.info(
                    "Consolidation triggered for user %d: oldest memory is %s days old",
                    user_id,
                    (now - oldest).days,
                )
                triggered = True
        elif (now - last_ts) > timedelta(days=TEMPORAL_THRESHOLD_DAYS):
            logging.info(
                "Consolidation triggered for user %d: %s days since last consolidation",
                user_id,
                (now - last_ts).days,
            )
            triggered = True

    if not triggered:
        return 0

    # Pass the already-fetched memories -- no second SELECT
    return await consolidate_memories(
        user_id,
        api_key,
        _prefetched_memories=raw_memories,
        expected_epoch=expected_epoch,
    )


async def _extract_persona_facts(memories_text: str, api_key: str) -> list[str]:
    """Use LLM to extract atomic persona facts from raw memories.

    Returns a list of concise fact strings (5-8 items).
    """
    graph = await _extract_graph(memories_text, api_key)
    return [
        fact.get("text", "") if isinstance(fact, dict) else str(fact)
        for fact in graph.get("facts", [])[:MAX_PERSONA_FACTS]
    ]


# ── GraphRAG extraction prompt ───────────────────────────────────────────────

_GRAPH_EXTRACTION_PROMPT = """Analyze these memory entries and extract a knowledge graph.

Output JSON with this exact schema:
{{
  "facts": [
    {{"text": "atomic persona fact 1", "source_ids": [123, 456]}}
  ],
  "entities": [
    {{"name": "Entity Name", "type": "person|project|skill|preference|place|concept", "description": "brief description"}}
  ],
  "relations": [
    {{"from": "Entity A", "to": "Entity B", "predicate": "relation verb phrase", "weight": 0.8, "is_core": false, "support_fact_indexes": [0]}}
  ]
}}

Rules:
- Extract {min_facts}-{max_facts} atomic facts about the user (identity, preferences, skills, goals, habits).
- Every fact must contain a non-empty source_ids list using only memory_id values shown below.
- Every relation must contain non-empty support_fact_indexes pointing to facts that support that relation.
- Extract ALL named entities: people, projects, technologies, places, preferences.
- Extract meaningful relations between entities.
- Entity names must be consistent and deduplicated.
- If two entries contradict, keep the NEWER information.
- weight: 0.0-1.0 confidence/strength of the relation.
- is_core: Set to TRUE only for PERMANENT identity facts that should NEVER be forgotten:
  user's real name, profession/job, permanent home location, chronic medical conditions,
  permanent disabilities, or other facts the user has explicitly stated are permanently true.
  Set to FALSE for preferences, habits, projects, opinions, goals — anything that can change.
- Write in the same language as the memories.
- Be concise. No speculation — only stated facts.

Memories:
{memories_text}"""


async def _extract_graph(memories_text: str, api_key: str) -> dict:
    """Extract facts + knowledge graph via structured JSON output.

    Returns dict with 'facts', 'entities', 'relations' keys.
    Falls back to empty graph on failure.
    """

    from google.genai import types

    from app.providers.gemini import get_cached_genai_client

    prompt = _GRAPH_EXTRACTION_PROMPT.format(
        memories_text=memories_text,
        min_facts=MIN_PERSONA_FACTS,
        max_facts=MAX_PERSONA_FACTS,
    )

    try:
        client = get_cached_genai_client(api_key)
        response = await client.aio.models.generate_content(
            model=_CONSOLIDATION_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=2048,
            ),
        )
        response_text = response.text or ""
        if not response_text.strip():
            logging.warning("Graph extraction returned empty response")
            return {"facts": [], "entities": [], "relations": []}
        result = json.loads(response_text)

        # Validate structure
        if not isinstance(result, dict):
            logging.warning("Graph extraction returned non-dict: %s", type(result))
            return {"facts": [], "entities": [], "relations": []}

        result.setdefault("facts", [])
        result.setdefault("entities", [])
        result.setdefault("relations", [])

        # Normalise is_core: must be a bool, default False
        for rel in result["relations"]:
            raw_core = rel.get("is_core", False)
            rel["is_core"] = bool(raw_core) if isinstance(raw_core, (bool, int)) else False

        n_core = sum(1 for r in result["relations"] if r.get("is_core"))
        logging.info(
            "Graph extraction: %d facts, %d entities, %d relations (%d core)",
            len(result["facts"]),
            len(result["entities"]),
            len(result["relations"]),
            n_core,
        )
        return result

    except Exception as e:
        logging.error("Graph extraction failed: %s", e, exc_info=True)
        # Fallback: try legacy plain-text extraction
        try:
            client = get_cached_genai_client(api_key)
            fallback_prompt = f"""Extract {MIN_PERSONA_FACTS}-{MAX_PERSONA_FACTS} atomic persona facts from these memories.
Write each fact on a separate line starting with "- ".

Memories:
{memories_text}

Extracted persona facts:"""
            response = await client.aio.models.generate_content(
                model=_CONSOLIDATION_MODEL,
                contents=fallback_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=1024,
                ),
            )
            text = (response.text or "").strip()
            facts = []
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith(("- ", "• ")):
                    facts.append(line[2:].strip())
            if not facts:
                facts = [ln.strip() for ln in text.split("\n") if ln.strip()]
            return {"facts": facts[:MAX_PERSONA_FACTS], "entities": [], "relations": []}
        except Exception as fallback_err:
            logging.error("Fallback fact extraction also failed: %s", fallback_err)
            return {"facts": [], "entities": [], "relations": []}


def _snapshot_matches(raw_memories: list[dict[str, Any]], current_rows: list[Any]) -> bool:
    """Return whether the current rows exactly match the extraction snapshot."""
    current_by_id = {int(row["id"]): row for row in current_rows}
    snapshot_by_id = {int(memory["id"]): memory for memory in raw_memories}
    if set(current_by_id) != set(snapshot_by_id):
        return False

    return all(
        all(
            current_by_id[memory_id][field] == original.get(field)
            for field in ("content", "source_type", "created_at", "expires_at")
        )
        for memory_id, original in snapshot_by_id.items()
    )


async def _preflight_consolidation_snapshot(
    user_id: int,
    raw_memories: list[dict[str, Any]],
    snapshot_epoch: int | None,
) -> bool:
    """Fail closed unless consent and the exact snapshot survived extraction."""
    raw_ids = sorted(int(memory["id"]) for memory in raw_memories)
    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            current_rows = await conn.fetch(
                """
                SELECT m.id, m.content, m.source_type, m.created_at, m.expires_at,
                       c.memory_epoch
                FROM long_term_memory m
                JOIN chats c ON c.user_id = m.user_id
                            AND c.ltm_enabled IS TRUE
                WHERE m.user_id = $1
                  AND m.id = ANY($2::bigint[])
                  AND ($3::bigint IS NULL OR c.memory_epoch = $3)
                  AND m.source_type != 'consolidated'
                  AND m.consolidated_at IS NULL
                  AND (m.expires_at IS NULL OR m.expires_at > now())
                ORDER BY m.id ASC
                """,
                user_id,
                raw_ids,
                snapshot_epoch,
            )
            return _snapshot_matches(raw_memories, current_rows)
    except Exception as error:
        logging.warning(
            "Consolidation snapshot preflight failed closed for user %d: %s",
            user_id,
            error,
        )
        return False


def _validate_graph_provenance(
    graph: dict[str, Any],
    raw_ids: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Validate exact raw→fact and fact→relation provenance from the LLM."""
    raw_facts = graph.get("facts")
    entities = graph.get("entities")
    relations = graph.get("relations")
    if (
        not isinstance(raw_facts, list)
        or not raw_facts
        or len(raw_facts) > MAX_PERSONA_FACTS
        or not isinstance(entities, list)
        or any(not isinstance(entity, dict) for entity in entities)
        or not isinstance(relations, list)
    ):
        return None

    raw_id_set = set(raw_ids)
    facts: list[dict[str, Any]] = []
    for raw_fact in raw_facts:
        if not isinstance(raw_fact, dict):
            return None
        text = raw_fact.get("text")
        source_ids = raw_fact.get("source_ids")
        if not isinstance(text, str) or not text.strip() or not isinstance(source_ids, list) or not source_ids:
            return None
        if any(type(source_id) is not int or source_id not in raw_id_set for source_id in source_ids):
            return None
        facts.append(
            {
                "text": text.strip(),
                "source_ids": list(dict.fromkeys(source_ids)),
            }
        )

    validated_relations: list[dict[str, Any]] = []
    for relation in relations:
        if not isinstance(relation, dict):
            return None
        support_indexes = relation.get("support_fact_indexes")
        if not isinstance(support_indexes, list) or not support_indexes:
            return None
        if any(type(index) is not int or index < 0 or index >= len(facts) for index in support_indexes):
            return None
        validated_relation = dict(relation)
        validated_relation["support_fact_indexes"] = list(dict.fromkeys(support_indexes))
        validated_relations.append(validated_relation)

    return facts, entities, validated_relations


async def consolidate_memories(
    user_id: int,
    api_key: str,
    *,
    _prefetched_memories: list[dict[str, Any]] | None = None,
    expected_epoch: int | None = None,
) -> int:
    """Lease raw-memory extraction, embeddings, and authoritative mutation."""
    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    if expected_epoch is None:
        expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="ltm:consolidation",
        require_ltm=True,
    ) as lease_current:
        if not lease_current:
            return 0
        return await _consolidate_memories_impl(
            user_id,
            api_key,
            _prefetched_memories=_prefetched_memories,
            expected_epoch=expected_epoch,
        )


async def _consolidate_memories_impl(
    user_id: int,
    api_key: str,
    *,
    _prefetched_memories: list[dict[str, Any]] | None = None,
    expected_epoch: int | None = None,
) -> int:
    """Perform memory consolidation with GraphRAG entity/relation extraction.

    1. Read all raw (non-consolidated) memories (skipped if _prefetched_memories supplied).
    2. Extract persona facts + knowledge graph (entities, relations) via LLM.
    3. Prepare and validate every required embedding before opening a transaction.
    4. Lock/revalidate the exact source snapshot and current memory consent.
    5. Insert consolidated facts and graph data, then mark sources consolidated.

    _prefetched_memories: pass already-fetched raw memories to avoid a second
    DB round-trip (used by maybe_consolidate). Do not pass from external callers.

    Returns number of new persona facts created, or 0 on failure.
    """
    # ⚡ Reuse pre-fetched data when available; fall back to DB fetch for direct calls.
    raw_memories = (
        _prefetched_memories
        if _prefetched_memories is not None
        else await get_raw_memories_for_consolidation(user_id, expected_epoch=expected_epoch)
    )
    if not raw_memories:
        return 0

    raw_ids = [int(memory["id"]) for memory in raw_memories]
    if len(raw_ids) != len(set(raw_ids)):
        logging.warning("Consolidation for user %d received a duplicate source snapshot", user_id)
        return 0

    snapshot_epochs = {memory.get("memory_epoch") for memory in raw_memories if memory.get("memory_epoch") is not None}
    if len(snapshot_epochs) > 1 or (expected_epoch is not None and snapshot_epochs != {expected_epoch}):
        logging.info("Skipping stale consolidation snapshot for user %d", user_id)
        return 0
    snapshot_epoch = expected_epoch if expected_epoch is not None else next(iter(snapshot_epochs), None)

    # Build text block for LLM
    lines = []
    for m in raw_memories:
        date_str = str(m["created_at"])[:10] if m.get("created_at") else "?"
        lines.append(f"[memory_id={int(m['id'])} date={date_str}] {m['content']}")
    memories_text = "\n".join(lines)

    # Extract graph (facts + entities + relations)
    graph = await _extract_graph(memories_text, api_key)
    if not await _preflight_consolidation_snapshot(user_id, raw_memories, snapshot_epoch):
        logging.info("Skipping stale consolidation after extraction for user %d", user_id)
        return 0

    validated_graph = _validate_graph_provenance(graph, raw_ids)
    if validated_graph is None:
        logging.warning("Consolidation for user %d produced invalid provenance — preserving sources", user_id)
        return 0
    fact_specs, entities, relations = validated_graph
    facts = [fact["text"] for fact in fact_specs]

    logging.info(
        "Consolidation for user %d: %d raw → %d facts, %d entities, %d relations",
        user_id,
        len(raw_memories),
        len(facts),
        len(entities),
        len(relations),
    )

    # Store consolidated facts + graph data in a transaction
    try:
        from app.repos.memory import _get_embedding

        # Pre-fetch all embeddings concurrently to avoid N+1 queries during transaction

        # Helper to gather with concurrency limit
        async def bounded_gather(tasks, limit=20):
            sem = asyncio.Semaphore(limit)

            async def run_task(task):
                async with sem:
                    return await task

            return await asyncio.gather(*(run_task(t) for t in tasks))

        # Build tasks for facts
        fact_tasks = [_get_embedding(fact, api_key, task_type="RETRIEVAL_DOCUMENT") for fact in facts]

        # Persist only entities that can be connected to a provenance-backed edge.
        # Entity-only model output has no retention anchor and must not create nodes.
        extracted_entity_names = {ent.get("name", "").strip() for ent in entities if ent.get("name", "").strip()}
        valid_relations = [
            rel
            for rel in relations
            if rel.get("from", "").strip() in extracted_entity_names
            and rel.get("to", "").strip() in extracted_entity_names
        ]
        relation_endpoint_names = {
            endpoint for rel in valid_relations for endpoint in (rel.get("from", "").strip(), rel.get("to", "").strip())
        }
        valid_entities = [ent for ent in entities if ent.get("name", "").strip() in relation_endpoint_names]
        # The consolidation schema provides exact support indexes for facts and
        # relations, but not for free-form entity descriptions. Embed/persist only
        # the relation endpoint name so an uncited description cannot leak across
        # source deletion boundaries.
        entity_texts = [ent.get("name", "").strip() for ent in valid_entities]
        entity_tasks = [_get_embedding(text, api_key, task_type="RETRIEVAL_DOCUMENT") for text in entity_texts]

        # Build tasks for relations
        relation_tasks = [
            _get_embedding(rel.get("predicate", "related_to").strip(), api_key, task_type="RETRIEVAL_DOCUMENT")
            for rel in valid_relations
        ]

        # Gather all embeddings
        all_embeddings = await bounded_gather(fact_tasks + entity_tasks + relation_tasks)

        if len(all_embeddings) != len(fact_tasks) + len(entity_tasks) + len(relation_tasks) or any(
            not embedding for embedding in all_embeddings
        ):
            logging.warning(
                "Consolidation for user %d aborted because a required embedding failed",
                user_id,
            )
            return 0

        # Unpack embeddings
        fact_embeddings = all_embeddings[: len(fact_tasks)]
        all_embeddings = all_embeddings[len(fact_tasks) :]
        entity_embeddings = all_embeddings[: len(entity_tasks)]
        all_embeddings = all_embeddings[len(entity_tasks) :]
        relation_embeddings = all_embeddings

        # Map entity and relation embeddings
        ent_emb_map = {
            ent.get("name", "").strip(): emb for ent, emb in zip(valid_entities, entity_embeddings, strict=False)
        }
        # Use relation tuple as key
        rel_emb_map = {
            (rel.get("from", "").strip(), rel.get("to", "").strip(), rel.get("predicate", "related_to").strip()): emb
            for rel, emb in zip(valid_relations, relation_embeddings, strict=False)
        }

        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)

            locked_sources = await conn.fetch(
                """
                SELECT m.id, m.content, m.source_type, m.created_at, m.expires_at,
                       c.memory_epoch
                FROM long_term_memory m
                JOIN chats c ON c.user_id = m.user_id
                            AND c.ltm_enabled IS TRUE
                WHERE m.user_id = $1
                  AND m.id = ANY($2::bigint[])
                  AND ($3::bigint IS NULL OR c.memory_epoch = $3)
                  AND m.source_type != 'consolidated'
                  AND m.consolidated_at IS NULL
                  AND (m.expires_at IS NULL OR m.expires_at > now())
                ORDER BY m.id ASC
                FOR UPDATE OF m
                FOR SHARE OF c
                """,
                user_id,
                sorted(raw_ids),
                snapshot_epoch,
            )

            if not _snapshot_matches(raw_memories, locked_sources):
                logging.info(
                    "Skipping stale/concurrent consolidation snapshot for user %d",
                    user_id,
                )
                return 0

            finite_expiries = [row["expires_at"] for row in locked_sources if row["expires_at"] is not None]
            conservative_expiry = min(finite_expiries, default=None)
            fact_embedding_strings = [
                f"[{','.join(str(value) for value in embedding)}]" for embedding in fact_embeddings
            ]
            inserted_fact_ids: list[int] = []
            for fact, fact_embedding_text in zip(facts, fact_embedding_strings, strict=True):
                row = await conn.fetchrow(
                    """
                    INSERT INTO long_term_memory
                        (user_id, content, embedding, source_type, metadata, expires_at)
                    VALUES ($1, $2, $3::halfvec, 'consolidated', '{}'::jsonb, $4)
                    RETURNING id
                    """,
                    user_id,
                    fact,
                    fact_embedding_text,
                    conservative_expiry,
                )
                if not row:
                    raise RuntimeError("consolidated fact insert returned no id")
                inserted_fact_ids.append(int(row["id"]))

            derivation_derived_ids: list[int] = []
            derivation_source_ids: list[int] = []
            for fact_index, derived_id in enumerate(inserted_fact_ids):
                for source_id in fact_specs[fact_index]["source_ids"]:
                    derivation_derived_ids.append(derived_id)
                    derivation_source_ids.append(source_id)
            await conn.execute(
                """
                INSERT INTO memory_derivation_sources
                    (derived_memory_id, source_memory_id, user_id)
                SELECT link.derived_memory_id, link.source_memory_id, $3
                FROM unnest($1::bigint[], $2::bigint[])
                     AS link(derived_memory_id, source_memory_id)
                ON CONFLICT (derived_memory_id, source_memory_id) DO NOTHING
                """,
                derivation_derived_ids,
                derivation_source_ids,
                user_id,
            )

            # Consolidated entity descriptions do not carry entity-level
            # support indexes, so only endpoint names and types are projected.
            entity_support_fact_ids: dict[str, set[int]] = {}
            for relation in valid_relations:
                support_ids = {inserted_fact_ids[index] for index in relation["support_fact_indexes"]}
                for endpoint in (relation.get("from", "").strip(), relation.get("to", "").strip()):
                    entity_support_fact_ids.setdefault(endpoint, set()).update(support_ids)
            node_candidate_list: list[GraphNodeCandidate] = []
            for entity in valid_entities:
                entity_name = entity.get("name", "").strip()
                entity_embedding = ent_emb_map.get(entity_name)
                node_candidate_list.append(
                    GraphNodeCandidate(
                        name=entity_name,
                        entity_type=str(entity.get("type", "concept")),
                        description="",
                        embedding=tuple(entity_embedding) if entity_embedding is not None else None,
                        wing="knowledge",
                        room="",
                        source_memory_ids=frozenset(entity_support_fact_ids[entity_name]),
                    )
                )
            node_candidates = tuple(node_candidate_list)

            edge_candidate_list: list[GraphEdgeCandidate] = []
            for relation in valid_relations:
                source_name = relation.get("from", "").strip()
                target_name = relation.get("to", "").strip()
                predicate = relation.get("predicate", "related_to").strip()
                relation_embedding = rel_emb_map.get((source_name, target_name, predicate))
                edge_candidate_list.append(
                    GraphEdgeCandidate(
                        source_name=source_name,
                        target_name=target_name,
                        predicate=predicate,
                        predicate_embedding=(tuple(relation_embedding) if relation_embedding is not None else None),
                        weight=float(relation.get("weight", 1.0)),
                        is_core=bool(relation.get("is_core", False)),
                        source_memory_ids=frozenset(
                            inserted_fact_ids[index] for index in relation["support_fact_indexes"]
                        ),
                    )
                )
            edge_candidates = tuple(edge_candidate_list)
            graph_result = await write_graph(
                conn,
                user_id,
                GraphMutationPlan(nodes=node_candidates, edges=edge_candidates),
            )

            marked_sources = await conn.fetch(
                """
                UPDATE long_term_memory
                SET consolidated_at = now()
                WHERE user_id = $1
                  AND id = ANY($2::bigint[])
                  AND consolidated_at IS NULL
                RETURNING id
                """,
                user_id,
                sorted(raw_ids),
            )
            marked_ids = {int(row["id"]) for row in marked_sources}
            if marked_ids != set(raw_ids):
                raise RuntimeError("consolidation source snapshot changed before completion")

            logging.info(
                "Consolidation complete for user %d: marked %d raw, inserted %d facts, %d nodes, %d edges",
                user_id,
                len(raw_ids),
                len(inserted_fact_ids),
                len(graph_result.node_ids),
                graph_result.edges_written,
            )
            return len(inserted_fact_ids)
    except Exception as e:
        logging.error("Consolidation transaction failed for user %d: %s", user_id, e, exc_info=True)
        return 0
