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
    prompt = f"""Analyze the following raw memory entries about a user and extract {MIN_PERSONA_FACTS}-{MAX_PERSONA_FACTS} atomic persona facts.

Rules:
- Each fact must be a single, self-contained statement about the user.
- Facts should cover: identity, preferences, skills, goals, relationships, habits.
- If two entries contradict each other, keep the NEWER information.
- Write each fact on a separate line, starting with "- ".
- Write in the same language as the majority of the memories.
- Be concise: each fact should be 1 sentence maximum.
- Do NOT include speculation or inferences — only stated facts.

Raw memories:
{memories_text}

Extracted persona facts:"""

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(_CONSOLIDATION_MODEL)
        response = await model.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        text = response.text.strip()

        # Parse bullet points
        facts = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith(("- ", "• ")):
                facts.append(line[2:].strip())

        if not facts:
            # Fallback: treat each non-empty line as a fact
            facts = [ln.strip() for ln in text.split("\n") if ln.strip()]

        return facts[:MAX_PERSONA_FACTS]

    except Exception as e:
        logging.error("Persona fact extraction failed: %s", e, exc_info=True)
        return []


async def consolidate_memories(user_id: int, api_key: str) -> int:
    """Perform memory consolidation for a user.

    1. Read all raw (non-consolidated) memories.
    2. Extract persona facts via LLM.
    3. Delete the raw batch.
    4. Insert consolidated facts.

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

    # Extract facts
    facts = await _extract_persona_facts(memories_text, api_key)
    if not facts:
        logging.warning("Consolidation for user %d produced no facts — skipping deletion", user_id)
        return 0

    logging.info(
        "Consolidation for user %d: %d raw memories -> %d persona facts",
        user_id,
        len(raw_memories),
        len(facts),
    )

    # Store consolidated facts and delete raw memories in a transaction
    try:
        from app.repos.memory import _get_embedding, store_memory

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

                    # Insert each consolidated fact
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

                    logging.info(
                        "Consolidation complete for user %d: deleted %d raw, inserted %d facts",
                        user_id,
                        len(raw_ids),
                        len(facts),
                    )
                    return len(facts)
            finally:
                await clear_user_context(conn=conn)
    except Exception as e:
        logging.error("Consolidation transaction failed for user %d: %s", user_id, e, exc_info=True)
        return 0
