# /app/context/compression.py
"""AAAK-style Tiered Memory Compression — MemPalace Integration.

Implements a 4-layer memory stack inspired by AAAK (lossless shorthand):

    L0: Core Facts     — JSON shorthand from is_core=TRUE graph edges (~200 tokens)
    L1: Active Context — structured summary from recent LTM + role diaries (~500 tokens)
    L2: Semantic Recall — search_memories_with_graph results (~1500 tokens)
    L3: Full History   — handled by assembler.py (remaining budget)

Each layer is independently cacheable and compressed to minimize token waste
while maximizing information density in the LLM's context window.
"""

import json
import logging
from typing import Any

from app.prompt_registry import estimate_tokens_cyrillic
from app.repos.memory_config import (
    L0_MAX_TOKENS,
    L1_MAX_TOKENS,
)

logger = logging.getLogger(__name__)


async def build_l0_facts(user_id: int, api_key: str) -> str:
    """Build L0 core facts from is_core=TRUE graph edges.

    Returns a compact JSON shorthand block that is always injected into
    system_instruction.  Example output:

        {"name":"Алексей","role":"dev","city":"Moscow","lang":["py","ts"]}

    Uses direct DB query (no embedding search needed — these are static facts).
    """
    try:
        from app.database import db_manager, db_query
        from app.repos.db_helpers import clear_user_context, set_user_context

        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                rows = await db_query(
                    """
                    SELECT src.entity_name AS from_name,
                           e.predicate,
                           tgt.entity_name AS to_name
                    FROM memory_edges e
                    JOIN memory_nodes src ON e.source_node = src.id
                    JOIN memory_nodes tgt ON e.target_node = tgt.id
                    WHERE e.user_id = $1
                      AND e.is_core = TRUE
                      AND e.valid_to IS NULL
                    ORDER BY e.weight DESC
                    LIMIT 20
                    """,
                    (user_id,),
                    conn=conn,
                )
            finally:
                await clear_user_context(conn=conn)

        if not rows:
            return ""

        # Compress into a JSON-like shorthand dict
        facts: dict[str, Any] = {}
        for row in rows:
            key = row["predicate"].replace(" ", "_").lower()[:20]
            value = row["to_name"]

            # Group multiple values for the same predicate
            if key in facts:
                existing = facts[key]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    facts[key] = [existing, value]
            else:
                facts[key] = value

        # Also add the subject name if available (the "self" node)
        if rows:
            facts.setdefault("_self", rows[0]["from_name"])

        shorthand = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))

        # Trim to L0 budget
        if estimate_tokens_cyrillic(shorthand) > L0_MAX_TOKENS:
            shorthand = shorthand[: L0_MAX_TOKENS * 3] + "}"

        return shorthand

    except Exception as exc:
        logger.debug("L0 core facts build failed: %s", exc)
        return ""


async def build_l1_context(
    user_id: int,
    api_key: str,
    *,
    role_id: str | None = None,
) -> str:
    """Build L1 active context from recent consolidated memories + role diary.

    Returns a structured summary block (~500 tokens) with:
    - Recent persona facts from consolidated memories
    - Active role diary entries (if role_id provided)
    """
    parts: list[str] = []

    # 1. Recent consolidated facts
    try:
        from app.database import db_manager, db_query
        from app.repos.db_helpers import clear_user_context, set_user_context

        async with db_manager.pool.acquire() as conn:
            await set_user_context(user_id, False, conn=conn)
            try:
                consolidated = await db_query(
                    """
                    SELECT content FROM long_term_memory
                    WHERE user_id = $1 AND source_type = 'consolidated'
                    ORDER BY created_at DESC
                    LIMIT 3
                    """,
                    (user_id,),
                    conn=conn,
                )
            finally:
                await clear_user_context(conn=conn)

        if consolidated:
            facts_block = " | ".join(r["content"][:200] for r in consolidated)
            parts.append(f"[Persona] {facts_block}")
    except Exception as exc:
        logger.debug("L1 consolidated facts failed: %s", exc)

    # 2. Role diary entries
    if role_id:
        try:
            from app.state import get_role_diary

            diary = get_role_diary(user_id, role_id)
            if diary:
                diary_text = " | ".join(diary[-5:])  # last 5 entries
                parts.append(f"[Diary:{role_id}] {diary_text}")
        except Exception as exc:
            logger.debug("L1 role diary failed: %s", exc)

    if not parts:
        return ""

    context = "\n".join(parts)

    # Trim to L1 budget
    if estimate_tokens_cyrillic(context) > L1_MAX_TOKENS:
        context = context[: L1_MAX_TOKENS * 3] + "..."

    return context


def format_compressed_context(
    l0: str,
    l1: str,
    l2_memories_xml: str,
    l2_graph_xml: str,
) -> str:
    """Format all memory layers into a single injection block.

    Returns XML-tagged block for system_instruction injection.
    """
    parts: list[str] = []

    if l0:
        parts.append(f"<core_identity>{l0}</core_identity>")

    if l1:
        parts.append(f"<active_context>{l1}</active_context>")

    if l2_memories_xml:
        parts.append(l2_memories_xml)

    if l2_graph_xml:
        parts.append(l2_graph_xml)

    if not parts:
        return ""

    return "\n<memory_palace>\n" + "\n".join(parts) + "\n</memory_palace>"


async def inject_memory_layers(
    user_id: int,
    query: str,
    api_key: str,
    system_instruction: str,
    *,
    role_id: str | None = None,
    limit: int = 5,
    min_similarity: float = 0.60,
) -> tuple[str, dict[str, int]]:
    """Inject all memory layers (L0-L2) into system_instruction.

    This replaces the inline memory injection block in ai_chat.py with
    a unified, tiered approach.

    Args:
        user_id: Telegram user ID.
        query: Current user message (for L2 semantic search).
        api_key: Gemini API key.
        system_instruction: Existing system prompt to augment.
        role_id: Active role ID for diary injection.
        limit: Max L2 memories to retrieve.
        min_similarity: Min cosine similarity for L2 search.

    Returns:
        (augmented_system_instruction, stats_dict)
    """
    stats: dict[str, int] = {
        "l0_tokens": 0,
        "l1_tokens": 0,
        "l2_memories": 0,
        "l2_graph_triples": 0,
    }

    # ── L0: Core Facts (always injected) ──────────────────────────────────
    l0 = await build_l0_facts(user_id, api_key)
    stats["l0_tokens"] = estimate_tokens_cyrillic(l0) if l0 else 0

    # ── L1: Active Context ────────────────────────────────────────────────
    l1 = await build_l1_context(user_id, api_key, role_id=role_id)
    stats["l1_tokens"] = estimate_tokens_cyrillic(l1) if l1 else 0

    # ── L2: Semantic Recall ───────────────────────────────────────────────
    l2_memories_xml = ""
    l2_graph_xml = ""

    if query and len(query) > 15:
        try:
            from app.handlers.chat_logic import format_memories_for_system_prompt
            from app.repos.memory import search_memories_with_graph

            memories, graph_triples, source_passages = await search_memories_with_graph(
                user_id,
                query,
                api_key,
                limit=limit,
                min_similarity=min_similarity,
            )

            if memories:
                l2_memories_xml = format_memories_for_system_prompt(memories) or ""
                stats["l2_memories"] = len(memories)

            if graph_triples:
                # Separate current and superseded triples
                current = [t for t in graph_triples if not t.startswith("[SUPERSEDED")]
                temporal = [t for t in graph_triples if t.startswith("[SUPERSEDED")]

                graph_parts = ["<knowledge_graph>"]
                for triple in current:
                    graph_parts.append(f"  {triple}")
                    # ── Edge Provenance: inject source passage for top-K edges ──
                    # Strip core/hop labels to match the triple key
                    _bare = triple.replace(" ★", "").replace(" (indirect)", "")
                    if _bare in source_passages:
                        graph_parts.append(
                            f"    <source_passage>{source_passages[_bare]}</source_passage>"
                        )

                if temporal:
                    graph_parts.append("\n  <temporal_context>")
                    for triple in temporal:
                        graph_parts.append(f"    {triple}")
                    graph_parts.append("  </temporal_context>")
                    graph_parts.append(
                        "  <temporal_instruction>"
                        "If you notice a factual change compared to superseded data "
                        "(e.g. new job, new city, health update, relationship change), "
                        "acknowledge it naturally — congratulate, empathize, or ask about it. "
                        "Если ты замечаешь, что факт изменился (новая работа, переезд, "
                        "изменение статуса), элегантно отметь это — поздравь, посочувствуй "
                        "или спроси об этом."
                        "</temporal_instruction>"
                    )

                graph_parts.append("</knowledge_graph>")
                l2_graph_xml = "\n".join(graph_parts)
                stats["l2_graph_triples"] = len(graph_triples)

            if not memories and not graph_triples:
                # ── LLM-as-judge fallback (RF-Mem "recollection path") ──
                try:
                    from app.repos.memory import search_memories_with_llm_judge

                    judged = await search_memories_with_llm_judge(
                        user_id,
                        query,
                        api_key,
                        limit=3,
                        candidate_floor=0.42,
                    )
                    if judged:
                        l2_memories_xml = format_memories_for_system_prompt(judged) or ""
                        stats["l2_memories"] = len(judged)
                except Exception as judge_exc:
                    logger.debug("LLM judge fallback failed: %s", judge_exc)

        except Exception as mem_err:
            logger.warning("L2 memory recall failed for user %d: %s", user_id, mem_err)

    # ── Compose final block ───────────────────────────────────────────────
    compressed = format_compressed_context(l0, l1, l2_memories_xml, l2_graph_xml)

    if compressed:
        system_instruction = system_instruction + "\n\n" + compressed

    total = sum(stats.values())
    if total > 0:
        logger.info(
            "MemPalace injected for user %d: L0=%d tok, L1=%d tok, L2=%d mem + %d graph",
            user_id,
            stats["l0_tokens"],
            stats["l1_tokens"],
            stats["l2_memories"],
            stats["l2_graph_triples"],
        )

    return system_instruction, stats
