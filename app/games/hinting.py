from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass

from app.games.ai_budget import HintGenerationMode, has_any_ai_studio_cooldown, should_pause_background_prefetch
from app.utils.background_tasks import start_background_task

logger = logging.getLogger(__name__)

_HINTS_INFLIGHT: dict[str, asyncio.Task[list[str] | None]] = {}
_BANK_PREFETCH_QUEUE: deque[_BankPrefetchItem] = deque()
_BANK_PREFETCH_PENDING_TOPICS: set[str] = set()
_BANK_PREFETCH_WORKER: asyncio.Task | None = None


@dataclass(frozen=True)
class _BankPrefetchItem:
    topic_key: str
    topic_id: str
    category: str
    words: tuple[str, ...]


def _hint_key(word: str, category: str, topic_id: str = "") -> str:
    return "\x00".join(((topic_id or "").strip().lower(), word.strip().lower(), category.strip().lower()))


async def get_or_generate_cached_hints(
    word: str,
    category: str,
    *,
    topic_id: str = "",
    mode: HintGenerationMode = "foreground",
) -> list[str] | None:
    from app.games.judge import generate_hints
    from app.games.judgement_cache import cache_hints, get_cached_hints

    cached = await get_cached_hints(word, category, topic_id=topic_id)
    if cached:
        return cached

    key = _hint_key(word, category, topic_id)
    inflight = _HINTS_INFLIGHT.get(key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    async def _do_generate() -> list[str] | None:
        hints = await generate_hints(word, category, mode=mode)
        if hints:
            await cache_hints(word, category, hints, topic_id=topic_id)
        return hints

    task = asyncio.create_task(_do_generate())
    _HINTS_INFLIGHT[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _HINTS_INFLIGHT.get(key) is task:
            _HINTS_INFLIGHT.pop(key, None)


def enqueue_bank_hint_prewarm(words: list[str], category: str, *, topic_id: str = "") -> bool:
    global _BANK_PREFETCH_WORKER

    if has_any_ai_studio_cooldown():
        logger.info("Skipping bank hint prewarm for category=%r because Gemini cooldown is active", category)
        return False

    trimmed = tuple(word for word in words[:2] if word)
    if not trimmed:
        return False

    topic_key = (topic_id or category).strip().lower()
    if topic_key in _BANK_PREFETCH_PENDING_TOPICS:
        return False

    _BANK_PREFETCH_PENDING_TOPICS.add(topic_key)
    _BANK_PREFETCH_QUEUE.append(
        _BankPrefetchItem(topic_key=topic_key, topic_id=topic_id, category=category, words=trimmed)
    )
    _BANK_PREFETCH_WORKER = start_background_task(
        _BANK_PREFETCH_WORKER,
        _run_bank_prefetch_worker,
        "croc_bank_hint_prefetch",
    )
    return True


async def _run_bank_prefetch_worker() -> None:
    global _BANK_PREFETCH_WORKER

    try:
        while _BANK_PREFETCH_QUEUE:
            if should_pause_background_prefetch():
                await asyncio.sleep(0.5)
                continue

            item = _BANK_PREFETCH_QUEUE.popleft()
            _BANK_PREFETCH_PENDING_TOPICS.discard(item.topic_key)
            for word in item.words:
                try:
                    await get_or_generate_cached_hints(
                        word,
                        item.category,
                        topic_id=item.topic_id,
                        mode="background",
                    )
                except Exception as exc:
                    logger.debug(
                        "Bank hint prewarm failed word=%r category=%r topic_id=%r: %s",
                        word,
                        item.category,
                        item.topic_id,
                        exc,
                    )
    finally:
        _BANK_PREFETCH_WORKER = None


def reset_hint_runtime_state_for_tests() -> None:
    global _BANK_PREFETCH_WORKER

    for task in list(_HINTS_INFLIGHT.values()):
        task.cancel()
    _HINTS_INFLIGHT.clear()
    _BANK_PREFETCH_QUEUE.clear()
    _BANK_PREFETCH_PENDING_TOPICS.clear()
    if _BANK_PREFETCH_WORKER is not None:
        _BANK_PREFETCH_WORKER.cancel()
    _BANK_PREFETCH_WORKER = None
