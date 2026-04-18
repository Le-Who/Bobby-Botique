"""
Boards repository — CRUD for Collaborative AI-Notes (inline_boards table).

The table is created by migration 035_create_inline_boards.sql.

Lazy bootstrap: if the table is somehow missing, _ensure_table() creates it
inline so the feature degrades gracefully rather than crashing the bot.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app import database as db
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

_TABLE_VERIFIED = False

# ── Schema bootstrap ──────────────────────────────────────────────────────────

_CREATE_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS inline_boards (
        id                          SERIAL PRIMARY KEY,
        inline_msg_id               TEXT        NOT NULL UNIQUE,
        chat_id                     BIGINT,
        message_id                  BIGINT,
        topic                       TEXT        NOT NULL,
        creator_id                  BIGINT      NOT NULL,
        entries                     JSONB       NOT NULL DEFAULT '[]',
        entries_since_last_synthesis INT         NOT NULL DEFAULT 0,
        last_summary                TEXT        NOT NULL DEFAULT '',
        closed                      BOOLEAN     NOT NULL DEFAULT FALSE,
        created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_inline_boards_inline_msg ON inline_boards (inline_msg_id);",
    "CREATE INDEX IF NOT EXISTS idx_inline_boards_chat_msg   ON inline_boards (chat_id, message_id) WHERE chat_id IS NOT NULL;",
]


async def _ensure_table() -> None:
    global _TABLE_VERIFIED
    if _TABLE_VERIFIED:
        return
    try:
        for stmt in _CREATE_DDL_STATEMENTS:
            await db.db_query(stmt)
        _TABLE_VERIFIED = True
    except Exception as exc:
        logger.warning("boards_repo: failed to bootstrap table: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────


async def create_board(
    inline_msg_id: str,
    topic: str,
    creator_id: int,
) -> int | None:
    """Insert a new board row. Returns the new board id, or None on error."""
    await _ensure_table()
    try:
        rows = await db.db_query(
            """
            INSERT INTO inline_boards (inline_msg_id, topic, creator_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (inline_msg_id) DO NOTHING
            RETURNING id
            """,
            (inline_msg_id, topic, creator_id),
        )
        return rows[0]["id"] if rows else None
    except Exception as exc:
        logger.error("boards_repo.create_board: %s", exc, exc_info=True)
        return None


async def link_board_to_chat(
    inline_msg_id: str,
    chat_id: int,
    message_id: int,
) -> bool:
    """Set chat_id and message_id for a board (called on first callback press).

    Returns True if a row was updated, False otherwise.
    """
    await _ensure_table()
    try:
        rows = await db.db_query(
            """
            UPDATE inline_boards
               SET chat_id    = $2,
                   message_id = $3,
                   updated_at = NOW()
             WHERE inline_msg_id = $1
               AND chat_id IS NULL   -- only set once; idempotent
            RETURNING id
            """,
            (inline_msg_id, chat_id, message_id),
        )
        return bool(rows)
    except Exception as exc:
        logger.error("boards_repo.link_board_to_chat: %s", exc, exc_info=True)
        return False


async def get_board_by_inline_msg(inline_msg_id: str) -> dict[str, Any] | None:
    """Return board row as dict, or None if not found / closed."""
    await _ensure_table()
    try:
        rows = await db.db_query(
            "SELECT * FROM inline_boards WHERE inline_msg_id = $1 AND NOT closed",
            (inline_msg_id,),
        )
        if not rows:
            return None
        row = dict(rows[0])
        # asyncpg returns JSONB as str on some drivers; normalise.
        if isinstance(row.get("entries"), str):
            row["entries"] = json.loads(row["entries"])
        return row
    except Exception as exc:
        logger.error("boards_repo.get_board_by_inline_msg: %s", exc, exc_info=True)
        return None


async def get_board_by_chat_msg(chat_id: int, message_id: int) -> dict[str, Any] | None:
    """Return board row by its chat position (used in reply routing)."""
    await _ensure_table()
    try:
        rows = await db.db_query(
            """
            SELECT * FROM inline_boards
             WHERE chat_id = $1 AND message_id = $2 AND NOT closed
            """,
            (chat_id, message_id),
        )
        if not rows:
            return None
        row = dict(rows[0])
        if isinstance(row.get("entries"), str):
            row["entries"] = json.loads(row["entries"])
        return row
    except Exception as exc:
        logger.error("boards_repo.get_board_by_chat_msg: %s", exc, exc_info=True)
        return None


async def add_entry(
    board_id: int,
    user_name: str,
    text: str,
) -> list[dict] | None:
    """Append one entry to the board and increment the new-entries counter.

    Returns the updated entries list, or None on error.
    """
    await _ensure_table()
    entry = {
        "user": user_name,
        "text": text[:1000],  # safety cap
        "ts": time.time(),
    }
    try:
        rows = await db.db_query(
            """
            UPDATE inline_boards
               SET entries                     = entries || $2::jsonb,
                   entries_since_last_synthesis = entries_since_last_synthesis + 1,
                   updated_at                  = NOW()
             WHERE id = $1 AND NOT closed
            RETURNING entries, entries_since_last_synthesis
            """,
            (board_id, json.dumps([entry])),  # append single element
        )
        if not rows:
            return None
        entries = rows[0]["entries"]
        if isinstance(entries, str):
            entries = json.loads(entries)
        return entries
    except Exception as exc:
        logger.error("boards_repo.add_entry board_id=%s: %s", board_id, exc, exc_info=True)
        return None


async def get_new_entries_count(board_id: int) -> int:
    """Return how many entries have been added since the last synthesis."""
    try:
        rows = await db.db_query(
            "SELECT entries_since_last_synthesis FROM inline_boards WHERE id = $1",
            (board_id,),
        )
        return rows[0]["entries_since_last_synthesis"] if rows else 0
    except Exception as exc:
        logger.error("boards_repo.get_new_entries_count: %s", exc, exc_info=True)
        return 0


async def update_summary(board_id: int, summary: str) -> bool:
    """Persist the latest LLM synthesis and reset the new-entries counter."""
    await _ensure_table()
    try:
        await db.db_query(
            """
            UPDATE inline_boards
               SET last_summary                 = $2,
                   entries_since_last_synthesis = 0,
                   updated_at                   = NOW()
             WHERE id = $1
            """,
            (board_id, summary),
        )
        return True
    except Exception as exc:
        logger.error("boards_repo.update_summary board_id=%s: %s", board_id, exc, exc_info=True)
        return False


async def close_board(board_id: int) -> bool:
    """Mark the board as closed (no further edits accepted)."""
    await _ensure_table()
    try:
        await db.db_query(
            "UPDATE inline_boards SET closed = TRUE, updated_at = NOW() WHERE id = $1",
            (board_id,),
        )
        return True
    except Exception as exc:
        logger.error("boards_repo.close_board board_id=%s: %s", board_id, exc, exc_info=True)
        return False


async def get_active_boards_count() -> int:
    """Return the number of non-closed boards (monitoring/health check)."""
    try:
        rows = await db.db_query("SELECT COUNT(*) AS cnt FROM inline_boards WHERE NOT closed")
        return rows[0]["cnt"] if rows else 0
    except Exception as exc:
        logger.error("boards_repo.get_active_boards_count: %s", exc, exc_info=True)
        return 0
