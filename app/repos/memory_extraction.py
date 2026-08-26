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
    5. Relations → exact-predicate memory_edges; semantic similarity is advisory only.
    6. Mutable node/edge attributes are snapshotted per exact LTM source.
    7. Temporal conflict: an old edge closes only after an explicit resolver
       verdict of ``update``; parallel/refinement predicates remain distinct.
"""

import asyncio
import logging
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from app.repos.memory_config import (
    GRAPH_EXTRACTION_MODEL,
    GRAPH_EXTRACTION_THINKING_LEVEL,
    MIN_EXTRACTION_LENGTH,
    TAXONOMY_WINGS,
    get_taxonomy_model,
)
from app.repos.memory_graph_writer import (
    GraphConflictClosure,
    GraphEdgeCandidate,
    GraphMutationPlan,
    GraphNodeCandidate,
    write_graph,
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
- Keep descriptions SHORT (max 12 words each) to fit within the token budget.

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
    import hashlib

    from app.errors import classify_key_error
    from app.handlers.ai_core import _resolve_ai_request
    from app.providers.gemini import get_cached_genai_client
    from app.repos.keys import get_key_status_manager

    prompt = _EXTRACTION_PROMPT.format(text=text[:4000])
    empty = GraphExtractionResult()
    status_mgr = get_key_status_manager()
    failed_keys: set[str] = set()

    for attempt in range(3):
        # Allow initial explicitly passed key to be used on attempt 0
        current_api_key = api_key if attempt == 0 else None

        if not current_api_key:
            key_data, _, _ = await _resolve_ai_request(
                GRAPH_EXTRACTION_MODEL, use_openrouter=False, excluded_key_hashes=failed_keys
            )
            if not key_data:
                logging.warning("Graph extraction exhausted available keys on attempt %d", attempt + 1)
                break
            current_api_key = key_data["api_key"]

        current_key_hash = hashlib.sha256(current_api_key.encode()).hexdigest()[:8]

        try:
            client = get_cached_genai_client(current_api_key)

            current_max_tokens = 2048
            if attempt > 0:
                current_max_tokens = 4096

            config_kwargs: dict = {
                "response_mime_type": "application/json",
                "response_json_schema": GraphExtractionResult.model_json_schema(),
                "temperature": 0.1,
                "max_output_tokens": current_max_tokens,
            }
            if GRAPH_EXTRACTION_THINKING_LEVEL and any(x in GRAPH_EXTRACTION_MODEL.lower() for x in ("pro", "think")):
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=GRAPH_EXTRACTION_THINKING_LEVEL,  # type: ignore[arg-type]
                )

            response = await client.aio.models.generate_content(
                model=GRAPH_EXTRACTION_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),  # type: ignore[arg-type]
            )
            response_text = (response.text or "").strip()
            if not response_text:
                logging.warning("Graph extraction returned empty response (attempt %d)", attempt + 1)
                return empty

            result = GraphExtractionResult.model_validate_json(response_text)

            # Success: update health
            try:
                await status_mgr.record_success(current_key_hash, GRAPH_EXTRACTION_MODEL)
            except Exception:
                pass

            logging.info(
                "Graph extraction: %d entities, %d relations",
                len(result.entities),
                len(result.relations),
            )
            return result

        except Exception as e:
            failed_keys.add(current_key_hash)
            error_str = str(e).lower()
            error_category = classify_key_error(error_str)

            # JSON truncation: Gemini cut the response mid-object due to max_output_tokens.
            # Treat as transient and retry with doubled token limit (no server-side wait needed).
            is_truncation = any(p in error_str for p in ("eof while parsing", "json_invalid", "unexpected end of"))
            is_transient = (
                error_category == "transient"
                or is_truncation
                or any(p in error_str for p in ("503", "unavailable", "overloaded", "rate limit", "timeout"))
            )

            if not is_truncation:  # Don't suspend for truncation — key is fine
                try:
                    if error_category != "permanent" or "api_key" in error_str or "400" in error_str:
                        await status_mgr.suspend_key(current_key_hash, GRAPH_EXTRACTION_MODEL, error_category, str(e))
                except Exception:
                    pass

            if is_transient and attempt < 2:
                if is_truncation:
                    # Token budget expands on next attempt logic explicitly (config max_tokens)
                    wait = 0.0
                    logging.warning(
                        "Graph extraction JSON truncated (key %s…, attempt %d) — retrying with 4096 tokens",
                        current_key_hash,
                        attempt + 1,
                    )
                else:
                    wait = (attempt + 1) * 2.0
                    logging.warning(
                        "Graph extraction transient error (key %s…, attempt %d, retrying in %.0fs): %s",
                        current_key_hash,
                        attempt + 1,
                        wait,
                        e,
                    )
                if wait > 0:
                    await asyncio.sleep(wait)
                continue
            logging.error("Graph extraction failed permanently with key %s…: %s", current_key_hash, e)
            continue  # Try next key for permanent failures as well

    return empty


async def _preflight_graph_source(
    user_id: int,
    source_memory_id: int | None,
    expected_epoch: int | None,
) -> bool:
    """Fail closed unless the graph source and durable consent are current.

    This intentionally performs no row locking or mutation.  Callers use it
    before external work, then retain their existing locked transaction check
    at the mutation boundary.
    """
    if source_memory_id is None:
        return False

    from app.database import db_manager
    from app.repos.db_helpers import set_user_context

    try:
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, False, conn=conn)
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM long_term_memory AS memory
                        JOIN chats AS chat ON chat.user_id = memory.user_id
                        WHERE memory.id = $2
                          AND memory.user_id = $1
                          AND chat.ltm_enabled IS TRUE
                          AND ($3::bigint IS NULL OR chat.memory_epoch = $3)
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    )
                    """,
                    user_id,
                    source_memory_id,
                    expected_epoch,
                )
            )
    except Exception as error:
        logging.warning(
            "Graph source preflight failed closed for user %d: %s",
            user_id,
            error,
        )
        return False


async def extract_and_store_graph(
    user_id: int,
    text: str,
    api_key: str,
    *,
    source_memory_id: int | None = None,
    chat_id: int | None = None,
    actor_user_id: int | None = None,
    expected_epoch: int | None = None,
) -> int:
    """Lease the full raw-text extraction and embedding/mutation chain."""
    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    if expected_epoch is None:
        expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="ltm:graph_extraction",
        require_ltm=True,
    ) as lease_current:
        if not lease_current:
            return 0
        return await _extract_and_store_graph_impl(
            user_id,
            text,
            api_key,
            source_memory_id=source_memory_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            expected_epoch=expected_epoch,
        )


async def _extract_and_store_graph_impl(
    user_id: int,
    text: str,
    api_key: str,
    *,
    source_memory_id: int | None = None,
    chat_id: int | None = None,
    actor_user_id: int | None = None,
    expected_epoch: int | None = None,
) -> int:
    """Extract knowledge graph from text and upsert into DB.

    This is the main entry point called from ai_chat._store_memory_in_background().
    A durable source memory is mandatory: zero-provenance graph writes are not
    retrievable and have no retention anchor, so they fail closed.

    Returns:
        Number of edges upserted.
    """
    if source_memory_id is None or len(text.strip()) < MIN_EXTRACTION_LENGTH:
        return 0

    if not await _preflight_graph_source(user_id, source_memory_id, expected_epoch):
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
        expected_epoch=expected_epoch,
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
    expected_epoch: int | None = None,
) -> int:
    """Atomically persist provenance-backed relation endpoints and edges.

    The first database phase is read-only and exists only to obtain an
    optimistic edge snapshot for external ambiguity resolution.  The pool
    connection is released before that external call.  A second short
    transaction rechecks and locks consent/source state, re-resolves nodes,
    upserts relation endpoints, re-reads current edges, and writes normalized
    provenance.  Any failure rolls the node and edge mutations back together.

    ``source_memory_id`` is required.  Derived writes without a durable source
    fail closed because neither expiry nor deletion could clean them safely.
    ``chat_id`` and ``actor_user_id`` remain accepted for API compatibility but
    do not relax the private-LTM provenance boundary.
    """
    del chat_id, actor_user_id

    if source_memory_id is None:
        logging.info("Skipping zero-provenance graph extraction for user %d", user_id)
        return 0

    if not await _preflight_graph_source(user_id, source_memory_id, expected_epoch):
        return 0

    from app.database import db_manager
    from app.repos.db_helpers import set_user_context
    from app.repos.memory import _get_embedding

    entities_by_name = {entity.name.strip(): entity for entity in graph.entities if entity.name.strip()}
    relation_specs: list[dict[str, Any]] = []
    for extracted_relation in graph.relations:
        source_name = extracted_relation.source.strip()
        target_name = extracted_relation.target.strip()
        predicate = extracted_relation.predicate.strip()
        if (
            not source_name
            or not target_name
            or not predicate
            or source_name not in entities_by_name
            or target_name not in entities_by_name
        ):
            continue
        relation_specs.append(
            {
                "relation": extracted_relation,
                "source_name": source_name,
                "target_name": target_name,
                "predicate": predicate,
            }
        )

    if not relation_specs:
        return 0

    endpoint_names = {
        endpoint
        for relation_spec in relation_specs
        for endpoint in (relation_spec["source_name"], relation_spec["target_name"])
    }
    endpoint_entities = [entity for entity in graph.entities if entity.name.strip() in endpoint_names]

    try:

        async def fetch_embedding(text: str) -> list[float] | None:
            return await _get_embedding(text, api_key, task_type="RETRIEVAL_DOCUMENT")

        entity_texts = [
            f"{entity.name.strip()}: {entity.description}" if entity.description else entity.name.strip()
            for entity in endpoint_entities
        ]
        embedding_tasks = [fetch_embedding(text) for text in entity_texts]
        embedding_tasks.extend(fetch_embedding(relation_spec["predicate"]) for relation_spec in relation_specs)
        embeddings = await asyncio.gather(*embedding_tasks)
        entity_embeddings = embeddings[: len(endpoint_entities)]
        relation_embeddings = embeddings[len(endpoint_entities) :]
        entity_embedding_map = {
            entity.name.strip(): embedding
            for entity, embedding in zip(endpoint_entities, entity_embeddings, strict=False)
        }
        for relation_spec, embedding in zip(relation_specs, relation_embeddings, strict=False):
            relation_spec["embedding"] = embedding
            relation_spec["embedding_text"] = (
                f"[{','.join(str(value) for value in embedding)}]" if embedding else None
            )

        async def consent_and_source_are_current(conn) -> bool:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM chats c
                        JOIN long_term_memory m
                          ON m.id = $3
                         AND m.user_id = c.user_id
                         AND (m.expires_at IS NULL OR m.expires_at > now())
                        WHERE c.user_id = $1
                          AND c.ltm_enabled IS TRUE
                          AND ($2::bigint IS NULL OR c.memory_epoch = $2)
                        FOR SHARE OF c
                        FOR KEY SHARE OF m
                    )
                    """,
                    user_id,
                    expected_epoch,
                    source_memory_id,
                )
            )

        async def resolve_existing_node(conn, entity_name: str) -> Any | None:
            embedding = entity_embedding_map.get(entity_name)
            if embedding:
                embedding_text = f"[{','.join(str(value) for value in embedding)}]"
                row = await conn.fetchrow(
                    """
                    SELECT id, entity_name
                    FROM memory_nodes
                    WHERE user_id = $1
                      AND embedding IS NOT NULL
                      AND embedding <=> $2::halfvec < 0.12
                    ORDER BY embedding <=> $2::halfvec ASC
                    LIMIT 1
                    """,
                    user_id,
                    embedding_text,
                )
                if row:
                    return row
            return await conn.fetchrow(
                """
                SELECT id, entity_name
                FROM memory_nodes
                WHERE user_id = $1
                  AND entity_name = $2
                LIMIT 1
                """,
                user_id,
                entity_name,
            )

        # Phase 1: read only.  It may produce external verdict inputs, but none
        # of its IDs are trusted for the mutation phase.
        phase_one_conflicts: dict[int, list[Any]] = {}
        async with db_manager.pool.acquire() as read_conn, read_conn.transaction():
            await set_user_context(user_id, False, conn=read_conn)
            if not await consent_and_source_are_current(read_conn):
                return 0

            existing_node_ids: dict[str, int] = {}
            for entity in endpoint_entities:
                original_name = entity.name.strip()
                row = await resolve_existing_node(read_conn, original_name)
                if row:
                    existing_node_ids[original_name] = int(row["id"])

            for relation_index, relation_spec in enumerate(relation_specs):
                source_id = existing_node_ids.get(relation_spec["source_name"])
                target_id = existing_node_ids.get(relation_spec["target_name"])
                embedding_text = relation_spec["embedding_text"]
                if source_id is None or target_id is None or embedding_text is None:
                    continue

                phase_one_conflicts[relation_index] = await read_conn.fetch(
                    """
                        SELECT id, predicate,
                               predicate_embedding <=> $4::halfvec AS distance
                        FROM memory_edges
                        WHERE user_id = $1
                          AND source_node = $2
                          AND target_node = $3
                          AND valid_to IS NULL
                          AND predicate_embedding IS NOT NULL
                          AND predicate <> $5
                        """,
                    user_id,
                    source_id,
                    target_id,
                    embedding_text,
                    relation_spec["predicate"],
                )

        # The read snapshot is no longer authoritative once its transaction is
        # released. Recheck durable consent immediately before constructing any
        # resolver coroutine so revoked private predicates/entities never reach
        # the external judge.
        if any(phase_one_conflicts.values()) and not await _preflight_graph_source(
            user_id,
            source_memory_id,
            expected_epoch,
        ):
            return 0

        resolution_keys: list[tuple[int, int, str]] = []
        resolution_tasks = []
        for relation_index, conflicts in phase_one_conflicts.items():
            relation_spec = relation_specs[relation_index]
            for old_edge in conflicts:
                old_predicate = str(old_edge["predicate"])
                resolution_keys.append((relation_index, int(old_edge["id"]), old_predicate))
                resolution_tasks.append(
                    _resolve_ambiguous_conflict(
                        old_predicate,
                        relation_spec["predicate"],
                        relation_spec["source_name"],
                        relation_spec["target_name"],
                        api_key,
                    )
                )
        resolution_values = await asyncio.gather(*resolution_tasks) if resolution_tasks else []
        resolutions = dict(zip(resolution_keys, resolution_values, strict=False))

        source_memory_ids = frozenset({source_memory_id})
        node_candidate_list: list[GraphNodeCandidate] = []
        for entity in endpoint_entities:
            entity_name = entity.name.strip()
            entity_embedding = entity_embedding_map.get(entity_name)
            node_candidate_list.append(
                GraphNodeCandidate(
                    name=entity_name,
                    entity_type=entity.type,
                    description=entity.description,
                    embedding=tuple(entity_embedding) if entity_embedding else None,
                    wing=entity.wing if entity.wing in TAXONOMY_WINGS else "knowledge",
                    room=entity.room or "",
                    source_memory_ids=source_memory_ids,
                )
            )
        node_candidates = tuple(node_candidate_list)
        edge_candidates: list[GraphEdgeCandidate] = []
        for relation_index, relation_spec in enumerate(relation_specs):
            conflict_closures = tuple(
                GraphConflictClosure(
                    edge_id=int(old_edge["id"]),
                    predicate=str(old_edge["predicate"]),
                )
                for old_edge in phase_one_conflicts.get(relation_index, [])
                if resolutions.get(
                    (
                        relation_index,
                        int(old_edge["id"]),
                        str(old_edge["predicate"]),
                    ),
                    "parallel",
                )
                == "update"
            )
            model_relation = relation_spec["relation"]
            relation_embedding = relation_spec["embedding"]
            edge_candidates.append(
                GraphEdgeCandidate(
                    source_name=relation_spec["source_name"],
                    target_name=relation_spec["target_name"],
                    predicate=relation_spec["predicate"],
                    predicate_embedding=tuple(relation_embedding) if relation_embedding else None,
                    weight=model_relation.weight,
                    is_core=model_relation.is_core,
                    source_memory_ids=source_memory_ids,
                    close_conflicts=conflict_closures,
                )
            )
        mutation_plan = GraphMutationPlan(
            nodes=node_candidates,
            edges=tuple(edge_candidates),
        )

        async with db_manager.pool.acquire() as write_conn, write_conn.transaction():
            await set_user_context(user_id, False, conn=write_conn)
            await write_conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            if not await consent_and_source_are_current(write_conn):
                return 0
            mutation_result = await write_graph(write_conn, user_id, mutation_plan)
            if not mutation_result.affected_edge_ids:
                raise RuntimeError("graph mutation produced no provenance-backed edges")

        edges_upserted = mutation_result.edges_written

        logging.info(
            "Real-time graph upsert for user %d: %d relation endpoints, %d edges",
            user_id,
            len(endpoint_entities),
            edges_upserted,
        )
        return edges_upserted
    except Exception as error:
        logging.error("Graph upsert failed for user %d: %s", user_id, error, exc_info=True)
        return 0


async def _resolve_ambiguous_conflict(
    old_predicate: str,
    new_predicate: str,
    source_name: str,
    target_name: str,
    api_key: str,
) -> str:
    """LLM judge for distinct exact predicates on the same entity pair.

    Embedding distance never decides temporal replacement by itself. This
    cheap Flash-Lite call is the only authority allowed to return ``update``;
    refinement remains a separate exact edge so its provenance is not mixed.

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
