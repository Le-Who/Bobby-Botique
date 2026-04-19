# /app/repos/memory_consolidation.py
"""Just-In-Time Memory Consolidation (Change 5).

Triggers consolidation when raw memories exceed a token threshold
(~8000 tokens) OR a temporal threshold (7+ days since last consolidation).
Uses a cheap LLM call to extract atomic "Persona Facts" from raw memories,
then replaces the batch with the consolidated facts.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.database import db_manager
from app.repos.db_helpers import clear_user_context, db_query, set_user_context
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


async def get_raw_memories_for_consolidation(user_id: int) -> list[dict[str, Any]]:
    """Return all non-consolidated memories for a user with token estimates."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                results = await db_query(
                    """
                    SELECT id, content, source_type, created_at
                    FROM long_term_memory
                    WHERE user_id = $1
                      AND source_type != 'consolidated'
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY created_at ASC
                    """,
                    (user_id,),
                    conn=conn,
                )
                return [
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "source_type": r["source_type"],
                        "created_at": r["created_at"],
                        "est_tokens": _estimate_tokens(r["content"]),
                    }
                    for r in (results or [])
                ]
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Failed to get raw memories for user %d: %s", user_id, e, exc_info=True)
        return []


async def get_last_consolidation_time(user_id: int) -> datetime | None:
    """Return the timestamp of the newest consolidated memory, or None."""
    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
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
            finally:
                await clear_user_context(conn=conn)
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


async def _extract_persona_facts(memories_text: str, api_key: str) -> list[str]:
    """Use LLM to extract atomic persona facts from raw memories.

    Returns a list of concise fact strings (5-8 items).
    """
    graph = await _extract_graph(memories_text, api_key)
    return graph.get("facts", [])[:MAX_PERSONA_FACTS]


# ── GraphRAG extraction prompt ───────────────────────────────────────────────

_GRAPH_EXTRACTION_PROMPT = """Analyze these memory entries and extract a knowledge graph.

Output JSON with this exact schema:
{{
  "facts": ["atomic persona fact 1", "fact 2", ...],
  "entities": [
    {{"name": "Entity Name", "type": "person|project|skill|preference|place|concept", "description": "brief description"}}
  ],
  "relations": [
    {{"from": "Entity A", "to": "Entity B", "predicate": "relation verb phrase", "weight": 0.8, "is_core": false}}
  ]
}}

Rules:
- Extract {min_facts}-{max_facts} atomic facts about the user (identity, preferences, skills, goals, habits).
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


async def consolidate_memories(user_id: int, api_key: str) -> int:
    """Perform memory consolidation with GraphRAG entity/relation extraction.

    1. Read all raw (non-consolidated) memories.
    2. Extract persona facts + knowledge graph (entities, relations) via LLM.
    3. Delete the raw batch.
    4. Insert consolidated facts into long_term_memory.
    5. Upsert entities into memory_nodes + relations into memory_edges.

    Returns number of new persona facts created, or 0 on failure.
    """
    raw_memories = await get_raw_memories_for_consolidation(user_id)
    if not raw_memories:
        return 0

    # Build text block for LLM
    lines = []
    for m in raw_memories:
        date_str = str(m["created_at"])[:10] if m.get("created_at") else "?"
        lines.append(f"[{date_str}] {m['content']}")
    memories_text = "\n".join(lines)

    # Extract graph (facts + entities + relations)
    graph = await _extract_graph(memories_text, api_key)
    facts = graph.get("facts", [])[:MAX_PERSONA_FACTS]
    entities = graph.get("entities", [])
    relations = graph.get("relations", [])

    if not facts:
        logging.warning("Consolidation for user %d produced no facts — skipping deletion", user_id)
        return 0

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
        import asyncio

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

        # Build tasks for entities
        valid_entities = [ent for ent in entities if ent.get("name", "").strip()]
        entity_texts = [
            f"{ent.get('name', '').strip()}: {ent.get('description', '')}"
            if ent.get("description", "")
            else ent.get("name", "").strip()
            for ent in valid_entities
        ]
        entity_tasks = [_get_embedding(text, api_key, task_type="RETRIEVAL_DOCUMENT") for text in entity_texts]

        # Build tasks for relations
        valid_relations = [rel for rel in relations if rel.get("from", "").strip() and rel.get("to", "").strip()]
        relation_tasks = [
            _get_embedding(rel.get("predicate", "related_to").strip(), api_key, task_type="RETRIEVAL_DOCUMENT")
            for rel in valid_relations
        ]

        # Gather all embeddings
        all_embeddings = await bounded_gather(fact_tasks + entity_tasks + relation_tasks)

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

        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                async with conn.transaction():
                    # Delete raw memories
                    raw_ids = [m["id"] for m in raw_memories]
                    await conn.execute(
                        "DELETE FROM long_term_memory WHERE user_id = $1 AND id = ANY($2::bigint[])",
                        user_id,
                        raw_ids,
                    )

                    # Insert each consolidated fact into long_term_memory
                    fact_records = []
                    for fact, embedding in zip(facts, fact_embeddings, strict=False):
                        if embedding is None:
                            continue
                        embedding_str = f"[{','.join(str(v) for v in embedding)}]"
                        fact_records.append((user_id, fact, embedding_str))

                    if fact_records:
                        await conn.executemany(
                            """
                            INSERT INTO long_term_memory (user_id, content, embedding, source_type, metadata)
                            VALUES ($1, $2, $3::halfvec, 'consolidated', '{}')
                            """,
                            fact_records,
                        )

                    # ── Upsert graph entities into memory_nodes ──────────
                    # Batch semantic deduplication for nodes
                    # First, gather unique entity names to avoid redundant DB lookups
                    unique_entity_names = {ent.get("name", "").strip() for ent in valid_entities if ent.get("name", "").strip()}

                    entities_with_emb = [
                        (name, f"[{','.join(str(v) for v in ent_emb_map[name])}]")
                        for name in unique_entity_names
                        if ent_emb_map.get(name) is not None
                    ]

                    name_mapping = {name: name for name in unique_entity_names}
                    if entities_with_emb:
                        input_names = [e[0] for e in entities_with_emb]
                        input_embs = [e[1] for e in entities_with_emb]
                        similar_nodes = await conn.fetch(
                            """
                            SELECT t.input_name, m.entity_name
                            FROM unnest($2::text[], $3::halfvec[]) AS t(input_name, emb)
                            LEFT JOIN LATERAL (
                                SELECT entity_name
                                FROM memory_nodes
                                WHERE user_id = $1
                                  AND embedding <=> t.emb < 0.12
                                ORDER BY embedding <=> t.emb ASC
                                LIMIT 1
                            ) m ON true
                            WHERE m.entity_name IS NOT NULL
                            """,
                            user_id,
                            input_names,
                            input_embs,
                        )
                        for row in similar_nodes:
                            name_mapping[row["input_name"]] = row["entity_name"]

                    # Prepare and deduplicate node upserts
                    final_node_upserts = {}  # name -> (type, desc, emb_str)
                    for ent in valid_entities:
                        orig_name = ent.get("name", "").strip()
                        canonical_name = name_mapping.get(orig_name, orig_name)
                        ent_type = ent.get("type", "concept")
                        description = ent.get("description", "")
                        ent_embedding = ent_emb_map.get(orig_name)
                        ent_emb_str = (
                            f"[{','.join(str(v) for v in ent_embedding)}]" if ent_embedding is not None else None
                        )

                        # In case multiple extractions map to same canonical name, last one wins
                        final_node_upserts[canonical_name] = (ent_type, description, ent_emb_str)

                    node_ids = {}  # entity_name → UUID
                    if final_node_upserts:
                        names = list(final_node_upserts.keys())
                        types = [v[0] for v in final_node_upserts.values()]
                        descs = [v[1] for v in final_node_upserts.values()]
                        embs = [v[2] for v in final_node_upserts.values()]

                        node_rows = await conn.fetch(
                            """
                            INSERT INTO memory_nodes (user_id, entity_name, entity_type, description, embedding)
                            SELECT $1, * FROM unnest($2::text[], $3::text[], $4::text[], $5::halfvec[])
                            ON CONFLICT (user_id, entity_name)
                            DO UPDATE SET
                                description = EXCLUDED.description,
                                entity_type = EXCLUDED.entity_type,
                                embedding = COALESCE(EXCLUDED.embedding, memory_nodes.embedding)
                            RETURNING id, entity_name
                            """,
                            user_id,
                            names,
                            types,
                            descs,
                            embs,
                        )
                        temp_node_ids = {r["entity_name"]: r["id"] for r in node_rows}
                        for orig, canon in name_mapping.items():
                            if canon in temp_node_ids:
                                node_ids[orig] = temp_node_ids[canon]

                    # ── Upsert graph relations into memory_edges ─────────
                    # Semantic Edge Deduplication (Change 4): Batch version
                    # Filter relations that have both nodes resolved
                    edge_candidates = []
                    for rel in valid_relations:
                        from_name = rel.get("from", "").strip()
                        to_name = rel.get("to", "").strip()
                        predicate = rel.get("predicate", "related_to").strip()
                        weight = float(rel.get("weight", 1.0))
                        is_core = bool(rel.get("is_core", False))

                        if from_name in node_ids and to_name in node_ids:
                            pred_embedding = rel_emb_map.get((from_name, to_name, predicate))
                            pred_emb_str = (
                                f"[{','.join(str(v) for v in pred_embedding)}]" if pred_embedding is not None else None
                            )
                            edge_candidates.append(
                                {
                                    "src_id": node_ids[from_name],
                                    "tgt_id": node_ids[to_name],
                                    "predicate": predicate,
                                    "weight": weight,
                                    "is_core": is_core,
                                    "emb": pred_emb_str,
                                }
                            )

                    if edge_candidates:
                        # Deduplicate candidates to avoid redundant DB work
                        # (src_id, tgt_id, predicate) is the unique constraint.
                        # We merge weight (max) and is_core (OR) to avoid data loss.
                        merged_edge_cands = {}
                        for cand in edge_candidates:
                            key = (cand["src_id"], cand["tgt_id"], cand["predicate"])
                            if key not in merged_edge_cands:
                                merged_edge_cands[key] = cand
                            else:
                                existing = merged_edge_cands[key]
                                existing["weight"] = max(existing["weight"], cand["weight"])
                                existing["is_core"] = existing["is_core"] or cand["is_core"]
                        edge_candidates = list(merged_edge_cands.values())

                        # Batch find similar edges
                        # We use unnest and LATERAL to find similar edges for all candidates in one query
                        src_ids = [c["src_id"] for c in edge_candidates]
                        tgt_ids = [c["tgt_id"] for c in edge_candidates]
                        embs = [c["emb"] for c in edge_candidates]

                        # Only those with embeddings can be semantically deduped
                        can_dedup = [i for i, emb in enumerate(embs) if emb is not None]
                        similar_edges_map = {}  # index -> similar_edge_id

                        if can_dedup:
                            dedup_src = [src_ids[i] for i in can_dedup]
                            dedup_tgt = [tgt_ids[i] for i in can_dedup]
                            dedup_emb = [embs[i] for i in can_dedup]

                            # This query finds the single most similar edge for each input triple
                            rows = await conn.fetch(
                                """
                                SELECT t.idx, m.id, m.predicate
                                FROM unnest($2::int[], $3::bigint[], $4::bigint[], $5::halfvec[]) AS t(idx, src, tgt, emb)
                                LEFT JOIN LATERAL (
                                    SELECT id, predicate
                                    FROM memory_edges
                                    WHERE user_id = $1
                                      AND source_node = t.src
                                      AND target_node = t.tgt
                                      AND predicate_embedding IS NOT NULL
                                      AND predicate_embedding <=> t.emb < 0.25
                                    ORDER BY predicate_embedding <=> t.emb ASC
                                    LIMIT 1
                                ) m ON true
                                WHERE m.id IS NOT NULL
                                """,
                                user_id,
                                can_dedup,
                                dedup_src,
                                dedup_tgt,
                                dedup_emb,
                            )
                            for r in rows:
                                similar_edges_map[r["idx"]] = (r["id"], r["predicate"])

                        # Batch updates (merges)
                        merges = []
                        new_inserts = []
                        for i, cand in enumerate(edge_candidates):
                            if i in similar_edges_map:
                                edge_id, old_pred = similar_edges_map[i]
                                merges.append((cand["weight"], cand["is_core"], edge_id))
                                logging.debug(
                                    "Semantic edge dedup: merged predicate '%s' into '%s'",
                                    cand["predicate"],
                                    old_pred,
                                )
                            else:
                                new_inserts.append(
                                    (
                                        user_id,
                                        cand["src_id"],
                                        cand["tgt_id"],
                                        cand["predicate"],
                                        cand["emb"],
                                        cand["weight"],
                                        cand["is_core"],
                                    )
                                )

                        if merges:
                            await conn.executemany(
                                """
                                UPDATE memory_edges
                                SET weight = $1,
                                    is_core = is_core OR $2,
                                    updated_at = now()
                                WHERE id = $3
                                """,
                                merges,
                            )

                        if new_inserts:
                            await conn.executemany(
                                """
                                INSERT INTO memory_edges
                                    (user_id, source_node, target_node, predicate,
                                     predicate_embedding, weight, is_core)
                                VALUES ($1, $2, $3, $4, $5::halfvec, $6, $7)
                                ON CONFLICT (user_id, source_node, target_node, predicate)
                                DO UPDATE SET
                                    weight = EXCLUDED.weight,
                                    is_core = memory_edges.is_core OR EXCLUDED.is_core,
                                    predicate_embedding = COALESCE(EXCLUDED.predicate_embedding, memory_edges.predicate_embedding),
                                    updated_at = now()
                                """,
                                new_inserts,
                            )

                    logging.info(
                        "Consolidation complete for user %d: deleted %d raw, inserted %d facts, %d nodes, %d edges",
                        user_id,
                        len(raw_ids),
                        len(facts),
                        len(node_ids),
                        len(
                            [
                                r
                                for r in relations
                                if r.get("from", "").strip() in node_ids and r.get("to", "").strip() in node_ids
                            ]
                        ),
                    )
                    return len(facts)
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Consolidation transaction failed for user %d: %s", user_id, e, exc_info=True)
        return 0
