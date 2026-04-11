# /app/repos/memory_extraction.py
"""Real-time graph knowledge extraction from user messages.

Replaces the batch-only consolidation graph extraction with a streaming
pipeline that fires on every qualifying user message.  Uses Gemini
Structured Outputs (Pydantic schema) + ThinkingConfig(medium) for
reliable, hallucination-resistant entity/relation extraction.

Architecture:
    1. User sends message → ai_chat._store_memory_in_background()
    2. After store_memory() succeeds, extract_and_store_graph() fires as
       a second background task.
    3. Gemini produces JSON conforming to GraphExtractionResult schema.
    4. Entities → memory_nodes (semantic dedup, cosine < 0.12).
    5. Relations → memory_edges (semantic predicate dedup, cosine < 0.25).
    6. Source provenance: source_memory_ids[] links edges to originating LTM rows.
    7. Temporal conflict: when a new edge conflicts with an existing one,
       the old edge is closed (valid_to = now()) and the new one is inserted.
"""

import logging
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from app.repos.memory_config import (
    EMBEDDING_DIMENSION,
    GRAPH_EXTRACTION_MODEL,
    GRAPH_EXTRACTION_THINKING_LEVEL,
    MIN_EXTRACTION_LENGTH,
    TAXONOMY_WINGS,
    get_taxonomy_model,
)

# ── Pydantic schemas for Structured Output ────────────────────────────────────


class ExtractedEntity(BaseModel):
    """A named entity extracted from user text."""

    name: str = Field(description="Canonical entity name (person, project, skill, place, concept)")
    type: str = Field(description="Entity type: person, project, skill, preference, place, concept, organization")
    description: str = Field(default="", description="Brief factual description of the entity")
    wing: str = Field(
        default="knowledge",
        description="MemPalace wing: identity, projects, social, knowledge, temporal",
    )
    room: str = Field(default="", description="MemPalace room within wing (e.g., bio, prefs, active)")


class ExtractedRelation(BaseModel):
    """A directed relationship between two entities."""

    source: str = Field(description="Source entity name (must match an entity in the entities list)")
    target: str = Field(description="Target entity name (must match an entity in the entities list)")
    predicate: str = Field(description="Relationship verb phrase (e.g. 'works at', 'likes', 'is friend of')")
    weight: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence/strength 0.0-1.0")
    is_core: bool = Field(
        default=False,
        description=(
            "TRUE only for PERMANENT identity facts that should NEVER be forgotten: "
            "real name, profession, permanent home, chronic conditions. "
            "FALSE for preferences, habits, projects, opinions, goals."
        ),
    )
    wing: str = Field(
        default="knowledge",
        description="MemPalace wing for this relation: identity, projects, social, knowledge, temporal",
    )


class GraphExtractionResult(BaseModel):
    """Structured output from graph extraction LLM call."""

    entities: list[ExtractedEntity] = Field(default_factory=list, description="Named entities found in the text")
    relations: list[ExtractedRelation] = Field(default_factory=list, description="Relations between entities")


# ── Extraction prompt ─────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """Analyze this user message and extract a knowledge graph.

Rules:
- Extract ALL named entities: people, projects, technologies, places, preferences, organizations.
- Extract meaningful directed relations between entities.
- Entity names must be consistent and deduplicated (use canonical forms).
- If the text is trivial (greetings, questions without facts), return empty lists.
- weight: 0.0-1.0 confidence/strength of the relation.
- is_core: TRUE ONLY for permanent identity facts (real name, profession, home location, medical conditions).
  FALSE for everything else (preferences, habits, projects, opinions, goals).
- wing: Classify each entity and relation into a MemPalace wing:
  * identity — personal facts (name, age, health, skills, values)
  * projects — work, coding, creative endeavors
  * social — people, relationships, organizations
  * knowledge — concepts, technologies, science
  * temporal — events, plans, routines, dates
- room: Subcategory within the wing (e.g., "bio", "prefs", "active", "family").
- Write names and predicates in the same language as the source text.
- Be concise. No speculation — only explicitly stated facts.

User message:
{text}"""


async def extract_graph_structured(
    text: str,
    api_key: str,
) -> GraphExtractionResult:
    """Extract entities and relations from text using Gemini Structured Output.

    Uses ThinkingConfig(medium) for improved reasoning about entity boundaries
    and relation directionality.  Falls back to empty result on any failure.

    Returns:
        Pydantic-validated GraphExtractionResult (never raises on API errors).
    """
    from app.providers.gemini import get_cached_genai_client

    prompt = _EXTRACTION_PROMPT.format(text=text[:4000])
    empty = GraphExtractionResult()

    for attempt in range(3):
        try:
            client = get_cached_genai_client(api_key)
            response = await client.aio.models.generate_content(
                model=GRAPH_EXTRACTION_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=GraphExtractionResult.model_json_schema(),
                    temperature=0.1,
                    max_output_tokens=2048,
                    thinking_config=types.ThinkingConfig(
                        thinking_level=GRAPH_EXTRACTION_THINKING_LEVEL,  # type: ignore[arg-type]
                    ),
                ),
            )
            response_text = (response.text or "").strip()
            if not response_text:
                logging.warning("Graph extraction returned empty response (attempt %d)", attempt + 1)
                return empty

            result = GraphExtractionResult.model_validate_json(response_text)

            logging.info(
                "Graph extraction: %d entities, %d relations",
                len(result.entities),
                len(result.relations),
            )
            return result

        except Exception as e:
            error_str = str(e).lower()
            is_transient = any(p in error_str for p in ("503", "unavailable", "overloaded", "rate limit", "timeout"))
            if is_transient and attempt < 2:
                import asyncio

                wait = (attempt + 1) * 2.0
                logging.warning(
                    "Graph extraction transient error (attempt %d, retrying in %.0fs): %s", attempt + 1, wait, e
                )
                await asyncio.sleep(wait)
                continue
            logging.error("Graph extraction failed permanently: %s", e, exc_info=True)
            return empty

    return empty


async def extract_and_store_graph(
    user_id: int,
    text: str,
    api_key: str,
    *,
    source_memory_id: int | None = None,
    chat_id: int | None = None,
    actor_user_id: int | None = None,
) -> int:
    """Extract knowledge graph from text and upsert into DB.

    This is the main entry point called from ai_chat._store_memory_in_background().
    For group chats, pass chat_id and actor_user_id for social graph attribution.

    Returns:
        Number of edges upserted.
    """
    if len(text.strip()) < MIN_EXTRACTION_LENGTH:
        return 0

    result = await extract_graph_structured(text, api_key)
    if not result.entities and not result.relations:
        return 0

    edges_upserted = await _upsert_graph(
        user_id,
        result,
        api_key,
        source_memory_id=source_memory_id,
        chat_id=chat_id,
        actor_user_id=actor_user_id,
    )
    return edges_upserted


async def _upsert_graph(
    user_id: int,
    graph: GraphExtractionResult,
    api_key: str,
    *,
    source_memory_id: int | None = None,
    chat_id: int | None = None,
    actor_user_id: int | None = None,
) -> int:
    """Upsert extracted entities and relations into memory_nodes/memory_edges.

    Implements:
    - Semantic Entity Resolution (cosine < 0.12) to merge near-identical names.
    - Semantic Edge Deduplication (cosine < 0.25) on predicates.
    - Temporal Conflict Management: closes old conflicting edges (valid_to = now()).
    - Source Provenance: appends source_memory_id to source_memory_ids[].
    - Social Graph: stores chat_id and actor_user_id when in group context.
    """
    from app.database import db_manager
    from app.repos.db_helpers import clear_user_context, set_user_context
    from app.repos.memory import _get_embedding

    edges_upserted = 0

    try:
        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                async with conn.transaction():
                    # ── Upsert entities ────────────────────────────────────
                    node_ids: dict[str, Any] = {}
                    for ent in graph.entities:
                        name = ent.name.strip()
                        if not name:
                            continue

                        ent_embedding = await _get_embedding(
                            f"{name}: {ent.description}" if ent.description else name,
                            api_key,
                            task_type="RETRIEVAL_DOCUMENT",
                        )
                        ent_emb_str = f"[{','.join(str(v) for v in ent_embedding)}]" if ent_embedding else None

                        # Semantic dedup: merge near-identical entity names
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
                                name = similar_node["entity_name"]

                        # Validate wing against allowed values
                        ent_wing = ent.wing if ent.wing in TAXONOMY_WINGS else "knowledge"
                        ent_room = ent.room or ""

                        try:
                            row = await conn.fetchrow(
                                """
                                INSERT INTO memory_nodes (user_id, entity_name, entity_type, description, embedding, wing, room)
                                VALUES ($1, $2, $3, $4, $5::halfvec, $6, $7)
                                ON CONFLICT (user_id, entity_name)
                                DO UPDATE SET
                                    description = CASE
                                        WHEN LENGTH(EXCLUDED.description) > LENGTH(memory_nodes.description)
                                        THEN EXCLUDED.description
                                        ELSE memory_nodes.description
                                    END,
                                    entity_type = EXCLUDED.entity_type,
                                    embedding = COALESCE(EXCLUDED.embedding, memory_nodes.embedding),
                                    wing = COALESCE(EXCLUDED.wing, memory_nodes.wing),
                                    room = COALESCE(EXCLUDED.room, memory_nodes.room),
                                    updated_at = now()
                                RETURNING id
                                """,
                                user_id,
                                name,
                                ent.type,
                                ent.description,
                                ent_emb_str,
                                ent_wing,
                                ent_room,
                            )
                        except Exception:
                            # Fallback: taxonomy columns not yet added (pre-migration 032)
                            row = await conn.fetchrow(
                                """
                                INSERT INTO memory_nodes (user_id, entity_name, entity_type, description, embedding)
                                VALUES ($1, $2, $3, $4, $5::halfvec)
                                ON CONFLICT (user_id, entity_name)
                                DO UPDATE SET
                                    description = CASE
                                        WHEN LENGTH(EXCLUDED.description) > LENGTH(memory_nodes.description)
                                        THEN EXCLUDED.description
                                        ELSE memory_nodes.description
                                    END,
                                    entity_type = EXCLUDED.entity_type,
                                    embedding = COALESCE(EXCLUDED.embedding, memory_nodes.embedding),
                                    updated_at = now()
                                RETURNING id
                                """,
                                user_id,
                                name,
                                ent.type,
                                ent.description,
                                ent_emb_str,
                            )
                        if row:
                            node_ids[ent.name.strip()] = row["id"]
                            # Also map canonical name if deduped
                            if name != ent.name.strip():
                                node_ids[name] = row["id"]

                    # ── Upsert relations ───────────────────────────────────
                    source_ids_arr = [source_memory_id] if source_memory_id else []

                    for rel in graph.relations:
                        src_name = rel.source.strip()
                        tgt_name = rel.target.strip()
                        if src_name not in node_ids or tgt_name not in node_ids:
                            continue

                        src_id = node_ids[src_name]
                        tgt_id = node_ids[tgt_name]

                        # Embed predicate for semantic dedup
                        pred_embedding = await _get_embedding(rel.predicate, api_key, task_type="RETRIEVAL_DOCUMENT")
                        pred_emb_str = f"[{','.join(str(v) for v in pred_embedding)}]" if pred_embedding else None

                        # Check for semantically similar existing edge
                        if pred_emb_str:
                            similar_edge = await conn.fetchrow(
                                """
                                SELECT id, predicate, weight
                                FROM memory_edges
                                WHERE user_id = $1
                                  AND source_node = $2
                                  AND target_node = $3
                                  AND predicate_embedding IS NOT NULL
                                  AND predicate_embedding <=> $4::halfvec < 0.25
                                  AND (valid_to IS NULL)
                                ORDER BY predicate_embedding <=> $4::halfvec ASC
                                LIMIT 1
                                """,
                                user_id,
                                src_id,
                                tgt_id,
                                pred_emb_str,
                            )
                            if similar_edge:
                                # Merge: update weight, keep is_core sticky, append source_memory_id
                                await conn.execute(
                                    """
                                    UPDATE memory_edges
                                    SET weight = $1,
                                        is_core = is_core OR $2,
                                        updated_at = now(),
                                        source_memory_ids = source_memory_ids || $3::bigint[]
                                    WHERE id = $4
                                    """,
                                    max(rel.weight, similar_edge["weight"]),
                                    rel.is_core,
                                    source_ids_arr,
                                    similar_edge["id"],
                                )
                                edges_upserted += 1
                                continue

                        # ── Temporal conflict: 2-stage gate (MemPalace) ────────
                        # Stage 1: Embedding distance triage
                        #   < 0.15  → near-duplicate, handled by merge above
                        #   0.15-0.35 → ambiguous zone → LLM judge
                        #   >= 0.35 → clearly different → close old edge
                        if pred_emb_str:
                            conflicting = await conn.fetch(
                                """
                                SELECT id, predicate,
                                       predicate_embedding <=> $4::halfvec AS distance
                                FROM memory_edges
                                WHERE user_id = $1
                                  AND source_node = $2
                                  AND target_node = $3
                                  AND valid_to IS NULL
                                  AND predicate_embedding IS NOT NULL
                                  AND predicate_embedding <=> $4::halfvec >= 0.15
                                """,
                                user_id,
                                src_id,
                                tgt_id,
                                pred_emb_str,
                            )
                            for old_edge in conflicting:
                                distance = float(old_edge.get("distance", 1.0))

                                if distance >= 0.35:
                                    # Stage 1: clearly different → close old
                                    await conn.execute(
                                        "UPDATE memory_edges SET valid_to = now() WHERE id = $1",
                                        old_edge["id"],
                                    )
                                    logging.info(
                                        "Temporal close: edge '%s' superseded by '%s' for user %d (dist=%.3f)",
                                        old_edge["predicate"],
                                        rel.predicate,
                                        user_id,
                                        distance,
                                    )
                                else:
                                    # Stage 2: ambiguous zone (0.15-0.35) → LLM judge
                                    verdict = await _resolve_ambiguous_conflict(
                                        old_edge["predicate"],
                                        rel.predicate,
                                        src_name,
                                        tgt_name,
                                        api_key,
                                    )
                                    if verdict == "update":
                                        await conn.execute(
                                            "UPDATE memory_edges SET valid_to = now() WHERE id = $1",
                                            old_edge["id"],
                                        )
                                        logging.info(
                                            "LLM judge: edge '%s' → '%s' = UPDATE for user %d",
                                            old_edge["predicate"],
                                            rel.predicate,
                                            user_id,
                                        )
                                    elif verdict == "refinement":
                                        # Merge into existing edge (update predicate text)
                                        await conn.execute(
                                            """
                                            UPDATE memory_edges
                                            SET predicate = $1,
                                                predicate_embedding = $2::halfvec,
                                                weight = GREATEST(weight, $3),
                                                updated_at = now()
                                            WHERE id = $4
                                            """,
                                            rel.predicate,
                                            pred_emb_str,
                                            rel.weight,
                                            old_edge["id"],
                                        )
                                        edges_upserted += 1
                                        logging.info(
                                            "LLM judge: edge '%s' → '%s' = REFINEMENT for user %d",
                                            old_edge["predicate"],
                                            rel.predicate,
                                            user_id,
                                        )
                                        continue  # skip insert below
                                    else:
                                        # parallel — keep both, no action on old
                                        logging.info(
                                            "LLM judge: edge '%s' ∥ '%s' = PARALLEL for user %d",
                                            old_edge["predicate"],
                                            rel.predicate,
                                            user_id,
                                        )

                        # Insert new edge
                        await conn.execute(
                            """
                            INSERT INTO memory_edges
                                (user_id, source_node, target_node, predicate,
                                 predicate_embedding, weight, is_core,
                                 source_memory_ids, valid_from)
                            VALUES ($1, $2, $3, $4, $5::halfvec, $6, $7, $8::bigint[], now())
                            ON CONFLICT (user_id, source_node, target_node, predicate)
                            DO UPDATE SET
                                weight = EXCLUDED.weight,
                                is_core = memory_edges.is_core OR EXCLUDED.is_core,
                                predicate_embedding = COALESCE(EXCLUDED.predicate_embedding,
                                                               memory_edges.predicate_embedding),
                                source_memory_ids = memory_edges.source_memory_ids || EXCLUDED.source_memory_ids,
                                updated_at = now()
                            """,
                            user_id,
                            src_id,
                            tgt_id,
                            rel.predicate,
                            pred_emb_str,
                            rel.weight,
                            rel.is_core,
                            source_ids_arr,
                        )
                        edges_upserted += 1

                logging.info(
                    "Real-time graph upsert for user %d: %d entities, %d edges",
                    user_id,
                    len(node_ids),
                    edges_upserted,
                )
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Graph upsert failed for user %d: %s", user_id, e, exc_info=True)

    return edges_upserted


async def _resolve_ambiguous_conflict(
    old_predicate: str,
    new_predicate: str,
    source_name: str,
    target_name: str,
    api_key: str,
) -> str:
    """LLM judge for ambiguous edge conflicts (cosine distance 0.15-0.35).

    When two predicates between the same entity pair are in the "grey zone"
    (not similar enough to merge, not different enough to supersede), this
    cheap Flash-Lite call determines the relationship:

    Returns one of:
        "update"     — factual change (close old, insert new)
        "parallel"   — both are simultaneously true (keep both)
        "refinement" — new is a more precise version of old (merge)
    """
    from app.providers.gemini import get_cached_genai_client

    prompt = (
        f"Two knowledge graph edges exist between '{source_name}' and '{target_name}':\n"
        f'  OLD: "{old_predicate}"\n'
        f'  NEW: "{new_predicate}"\n\n'
        "Classify the relationship between OLD and NEW as exactly one of:\n"
        "  update — NEW replaces OLD (factual change, e.g. new job, new city)\n"
        "  parallel — both are simultaneously true (e.g. likes Python AND likes TypeScript)\n"
        "  refinement — NEW is a more precise version of OLD (merge them)\n\n"
        "Output ONLY the word: update, parallel, or refinement."
    )

    try:
        client = get_cached_genai_client(api_key)
        response = await client.aio.models.generate_content(
            model=get_taxonomy_model(),
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=10,
            ),
        )
        answer = (response.text or "").strip().lower()
        if answer in ("update", "parallel", "refinement"):
            return answer
        logging.debug("LLM judge returned unexpected: %r, defaulting to 'parallel'", answer)
        return "parallel"
    except Exception as exc:
        logging.debug("LLM judge failed (non-critical): %s", exc)
        return "parallel"  # safe default: keep both edges
