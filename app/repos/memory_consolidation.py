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

import google.generativeai as genai

from app.database import db_manager
from app.repos.db_helpers import clear_user_context, db_query, set_user_context

# Approximate tokens per character for mixed Cyrillic/Latin text
_CHARS_PER_TOKEN = 3.5
TOKEN_THRESHOLD = 8000
TEMPORAL_THRESHOLD_DAYS = 7
MAX_PERSONA_FACTS = 8
MIN_PERSONA_FACTS = 5

# Consolidation model — use cheapest available free-tier model
_CONSOLIDATION_MODEL = "gemini-3.1-flash-lite-preview"

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
    import json

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
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(_CONSOLIDATION_MODEL)
            fallback_prompt = f"""Extract {MIN_PERSONA_FACTS}-{MAX_PERSONA_FACTS} atomic persona facts from these memories.
Write each fact on a separate line starting with "- ".

Memories:
{memories_text}

Extracted persona facts:"""
            response = await model.generate_content_async(
                fallback_prompt,
                generation_config=genai.types.GenerationConfig(
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
        from app.repos.memory import _get_embedding

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
                    for fact in facts:
                        embedding = await _get_embedding(fact, api_key, task_type="RETRIEVAL_DOCUMENT")
                        if embedding is None:
                            continue
                        embedding_str = f"[{','.join(str(v) for v in embedding)}]"
                        await conn.execute(
                            """
                            INSERT INTO long_term_memory (user_id, content, embedding, source_type, metadata)
                            VALUES ($1, $2, $3::halfvec, 'consolidated', '{}')
                            """,
                            user_id,
                            fact,
                            embedding_str,
                        )

                    # ── Upsert graph entities into memory_nodes ──────────
                    node_ids = {}  # entity_name → UUID
                    for ent in entities:
                        name = ent.get("name", "").strip()
                        if not name:
                            continue
                        ent_type = ent.get("type", "concept")
                        description = ent.get("description", "")

                        # Generate embedding for the entity
                        ent_embedding = await _get_embedding(
                            f"{name}: {description}" if description else name,
                            api_key,
                            task_type="RETRIEVAL_DOCUMENT",
                        )
                        ent_emb_str = f"[{','.join(str(v) for v in ent_embedding)}]" if ent_embedding else None

                        # Semantic Deduplication: Check if a highly similar node already exists
                        if ent_emb_str:
                            similar_node = await conn.fetchrow(
                                """
                                SELECT id, entity_name 
                                FROM memory_nodes 
                                WHERE user_id = $1 
                                  AND embedding <=> $2::halfvec < 0.12
                                ORDER BY embedding <=> $2::halfvec ASC
                                LIMIT 1
                                """,
                                user_id,
                                ent_emb_str,
                            )
                            if similar_node:
                                # A semantically identical node exists. Use its canonical name.
                                # This merges variations like "Tony Stark" and "Тони Старк" without duplicating.
                                name = similar_node["entity_name"]

                        row = await conn.fetchrow(
                            """
                            INSERT INTO memory_nodes (user_id, entity_name, entity_type, description, embedding)
                            VALUES ($1, $2, $3, $4, $5::halfvec)
                            ON CONFLICT (user_id, entity_name)
                            DO UPDATE SET
                                description = EXCLUDED.description,
                                entity_type = EXCLUDED.entity_type,
                                embedding = COALESCE(EXCLUDED.embedding, memory_nodes.embedding),
                                updated_at = now()
                            RETURNING id
                            """,
                            user_id,
                            name,
                            ent_type,
                            description,
                            ent_emb_str,
                        )
                        if row:
                            node_ids[name] = row["id"]

                    # ── Upsert graph relations into memory_edges ─────────
                    # Semantic Edge Deduplication (Change 4): before inserting each
                    # edge, check if a semantically similar predicate already exists
                    # between the same pair of nodes (cosine distance < 0.25).
                    # If so, update the existing edge's weight rather than adding a dupe.
                    for rel in relations:
                        from_name = rel.get("from", "").strip()
                        to_name = rel.get("to", "").strip()
                        predicate = rel.get("predicate", "related_to").strip()
                        weight = float(rel.get("weight", 1.0))
                        is_core = bool(rel.get("is_core", False))  # Core Persona (Change 5)

                        if from_name not in node_ids or to_name not in node_ids:
                            continue  # Skip relations with missing entities

                        src_id = node_ids[from_name]
                        tgt_id = node_ids[to_name]

                        # Semantic dedup: look for an existing edge between the same
                        # node pair whose predicate vector is close (< 0.25 distance).
                        # We embed the new predicate and compare against all existing
                        # predicates for this src→tgt pair.
                        pred_embedding = await _get_embedding(predicate, api_key, task_type="RETRIEVAL_DOCUMENT")
                        if pred_embedding:
                            pred_emb_str = f"[{','.join(str(v) for v in pred_embedding)}]"
                            similar_edge = await conn.fetchrow(
                                """
                                SELECT id, predicate
                                FROM memory_edges
                                WHERE user_id = $1
                                  AND source_node = $2
                                  AND target_node = $3
                                  AND predicate_embedding IS NOT NULL
                                  AND predicate_embedding <=> $4::halfvec < 0.25
                                ORDER BY predicate_embedding <=> $4::halfvec ASC
                                LIMIT 1
                                """,
                                user_id, src_id, tgt_id, pred_emb_str,
                            )
                            if similar_edge:
                                # Merge into existing edge — update weight & is_core
                                await conn.execute(
                                    """
                                    UPDATE memory_edges
                                    SET weight = $1,
                                        is_core = is_core OR $2,
                                        updated_at = now()
                                    WHERE id = $3
                                    """,
                                    weight, is_core, similar_edge["id"],
                                )
                                logging.debug(
                                    "Semantic edge dedup: merged predicate '%s' into '%s'",
                                    predicate, similar_edge["predicate"],
                                )
                                continue
                        else:
                            pred_emb_str = None

                        await conn.execute(
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
                            user_id, src_id, tgt_id, predicate,
                            pred_emb_str, weight, is_core,
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
