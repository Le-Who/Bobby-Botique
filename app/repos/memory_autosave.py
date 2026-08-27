# /app/repos/memory_autosave.py
"""Auto-save hooks for memory subsystem — MemPalace Safety Layer.

Ensures state persistence through three mechanisms:
1. **Heartbeat**: Periodic chat_state flush every N messages.
2. **Pre-shutdown compact**: Drains in-flight memory consolidation on SIGTERM.
3. **Memory write drain**: Awaits all background store_memory / graph tasks.

Architecture:
    bot.py:_cleanup_application() → drain_pending_memory_writes()
                                   → pre_shutdown_compact()
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.database import ChatState

logger = logging.getLogger(__name__)

# ── Heartbeat Configuration ─────────────────────────────────────────────────
HEARTBEAT_INTERVAL = 10  # Flush chat_state every N messages

# ── In-flight tracking ──────────────────────────────────────────────────────
# Set of asyncio.Tasks that are currently writing to LTM or extracting graph.
# Populated by _store_memory_in_background via register_memory_task().
_inflight_memory_tasks: set[asyncio.Task] = set()
_inflight_memory_tasks_by_user: dict[int, set[asyncio.Task]] = {}


def register_memory_task(task: asyncio.Task, user_id: int | None = None) -> None:
    """Register a background memory write task for shutdown draining.

    Called from ai_chat._store_memory_in_background() so that
    drain_pending_memory_writes() can await all in-flight writes.
    """
    _inflight_memory_tasks.add(task)
    task.add_done_callback(_inflight_memory_tasks.discard)
    if user_id is not None:
        user_tasks = _inflight_memory_tasks_by_user.setdefault(user_id, set())
        user_tasks.add(task)

        def _discard_user_task(done: asyncio.Task) -> None:
            tasks = _inflight_memory_tasks_by_user.get(user_id)
            if tasks is None:
                return
            tasks.discard(done)
            if not tasks:
                _inflight_memory_tasks_by_user.pop(user_id, None)

        task.add_done_callback(_discard_user_task)


def submit_memory_task(user_id: int, factory, *, retry: int = 3) -> asyncio.Task:
    """Submit and track a retryable LTM mutation for one user."""
    from app.utils.background_tasks import submit_retryable

    task = submit_retryable(factory, retry=retry)
    register_memory_task(task, user_id)
    return task


async def cancel_user_memory_tasks(user_id: int) -> int:
    """Cancel local queued/in-flight LTM tasks before a user-wide erase.

    The durable epoch check remains authoritative across processes.  This local
    cancellation only shortens the erase window and avoids wasted API work.
    """
    current = asyncio.current_task()
    tasks = [
        task for task in _inflight_memory_tasks_by_user.get(user_id, set()) if task is not current and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


async def heartbeat_save(
    user_id: int,
    msg_count: int,
    chat_state: ChatState,
) -> None:
    """Periodic chat_state flush — fire-and-forget every HEARTBEAT_INTERVAL messages.

    This ensures that even without explicit context truncation, the chat_state
    (including context_summary) is persisted periodically.  On the dedicated
    host (4GB RAM), this adds negligible overhead.

    Args:
        user_id: Telegram user ID.
        msg_count: Current message count in this session (monotonic).
        chat_state: The ChatState object to persist.
    """
    if msg_count % HEARTBEAT_INTERVAL != 0:
        return

    try:
        from app.repos.chats import update_user_chat

        await update_user_chat(user_id, chat_state)
        logger.debug("Heartbeat save for user %d at message #%d", user_id, msg_count)
    except Exception as exc:
        logger.warning("Heartbeat save failed for user %d: %s", user_id, exc)


async def pre_shutdown_compact(timeout: float = 8.0) -> int:
    """Flush partially-accumulated consolidation state before shutdown.

    When SIGTERM fires, some users may have msg_count between 1 and _MSG_GATE-1
    (i.e., they haven't hit the consolidation check threshold yet).  For those
    users with >= half the gate threshold, we fire a quick consolidation check.

    Returns:
        Number of users for which consolidation was attempted.
    """
    try:
        from app.repos.memory_consolidation import (
            _MSG_GATE,
            _consolidation_state,
            maybe_consolidate,
        )

        half_gate = _MSG_GATE // 2
        candidates = [uid for uid, state in _consolidation_state.items() if state.get("msg_count", 0) >= half_gate]

        if not candidates:
            logger.info("Pre-shutdown compact: no candidates (all below half-gate)")
            return 0

        logger.info("Pre-shutdown compact: %d candidate users", len(candidates))

        async def _try_consolidate(uid: int) -> None:
            try:
                from app.repos.keys import get_available_gemini_key
                from app.repos.memory_config import EMBEDDING_MODEL

                # ⚡ maybe_consolidate fetches memories once for both check + consolidation
                key_data = await get_available_gemini_key(model_name=EMBEDDING_MODEL)
                if key_data:
                    result = await maybe_consolidate(uid, key_data["api_key"])
                    if result:
                        logger.info("Pre-shutdown consolidation completed for user %d (%d facts)", uid, result)
            except Exception as exc:
                logger.warning("Pre-shutdown consolidation failed for user %d: %s", uid, exc)

        tasks = [asyncio.create_task(_try_consolidate(uid)) for uid in candidates]
        done, pending = await asyncio.wait(tasks, timeout=timeout)

        for task in pending:
            task.cancel()

        return len(done)

    except Exception as exc:
        logger.warning("Pre-shutdown compact failed: %s", exc)
        return 0


async def drain_pending_memory_writes(timeout: float = 5.0) -> None:
    """Await all in-flight store_memory / extract_and_store_graph tasks.

    Called during graceful shutdown to ensure no memory writes are lost
    mid-flight when the process exits.
    """
    if not _inflight_memory_tasks:
        logger.info("No in-flight memory writes to drain")
        return

    count = len(_inflight_memory_tasks)
    logger.info("Draining %d in-flight memory write tasks...", count)

    try:
        done, pending = await asyncio.wait(
            list(_inflight_memory_tasks),
            timeout=timeout,
        )
        if pending:
            logger.warning(
                "Memory drain: %d/%d tasks completed, %d timed out",
                len(done),
                count,
                len(pending),
            )
            for task in pending:
                task.cancel()
        else:
            logger.info("Memory drain: all %d tasks completed", count)
    except Exception as exc:
        logger.warning("Memory drain failed: %s", exc)
