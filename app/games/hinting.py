from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass

from app.games.ai_budget import HintGenerationMode, has_any_ai_studio_cooldown, should_pause_background_prefetch
from app.games.crocodile_flags import is_hint_prewarm_enabled
from app.utils.background_tasks import start_background_task
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

_SPACES_RE = re.compile(r"\s+")

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
    topic_part = (topic_id or "").strip().lower()
    if topic_part:
        return "\x00".join((topic_part, word.strip().lower()))
    return "\x00".join((topic_part, word.strip().lower(), category.strip().lower()))


def _dedupe_hint_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = _SPACES_RE.sub(" ", item).strip().strip("\"'`")
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _normalize_batch_word(word: str) -> str:
    return _SPACES_RE.sub(" ", word).strip().lower()


def _pick_batch_hint_model(settings_obj: object | None) -> str | None:
    if settings_obj is None:
        return None
    available = set(getattr(settings_obj, "OPENCODE_AVAILABLE_MODELS", []) or [])
    candidates = (
        "opencode-go/glm-5.1",
        getattr(settings_obj, "OPENCODE_QNA_MODEL", None),
        "opencode-go/qwen3.6-plus",
        "opencode-go/glm-5",
        getattr(settings_obj, "OPENCODE_DEFAULT_MODEL", None),
        "opencode-go/kimi-k2.5",
        "opencode-go/qwen3.5-plus",
    )
    if available:
        for candidate in candidates:
            if candidate and candidate in available:
                return candidate
        return None
    for candidate in candidates:
        if candidate:
            return candidate
    return None


def _extract_batched_hints(response_text: str, requested_words: tuple[str, ...]) -> dict[str, list[str]]:
    cleaned = response_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}

    if isinstance(payload, dict):
        raw_items = payload.get("items", [])
    elif isinstance(payload, list):
        raw_items = payload
    else:
        return {}
    if not isinstance(raw_items, list):
        return {}

    requested = {_normalize_batch_word(word): word for word in requested_words}
    accepted: dict[str, list[str]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_word = item.get("word")
        raw_hints = item.get("hints")
        if not isinstance(raw_word, str) or not isinstance(raw_hints, list):
            continue
        normalized_word = _normalize_batch_word(raw_word)
        if normalized_word not in requested or normalized_word in accepted:
            continue
        hints = _dedupe_hint_items([str(hint) for hint in raw_hints if isinstance(hint, str | int | float)])
        if len(hints) != 3:
            continue
        accepted[normalized_word] = hints
    return accepted


async def _generate_batched_hints(words: tuple[str, ...], category: str) -> dict[str, list[str]]:
    if len(words) < 2:
        return {}

    import app.config as config_module
    from app.errors import classify_key_error, extract_retry_after_seconds, is_error_message, strip_error_tag
    from app.games.ai_budget import acquire_background_slot, record_result
    from app.providers import get_provider_router

    settings_obj = getattr(config_module, "settings", None)
    model_name = _pick_batch_hint_model(settings_obj)
    if not model_name:
        return {}

    c_str = f" (категория: {category})" if category and "особое" not in category.lower() else ""
    word_lines = "\n".join(f"- {word}" for word in words)
    prompt = (
        "Игра «Крокодил».\n"
        f"Ниже слова одной темы{c_str}:\n{word_lines}\n\n"
        "Для КАЖДОГО слова верни отдельные 3 подсказки на русском языке.\n"
        'Ответь ТОЛЬКО JSON в формате {"items":[{"word":"...","hints":["...","...","..."]}]}.'
        "\nПравила:\n"
        "- В items должны быть записи только для перечисленных слов.\n"
        "- Подсказки слова A не должны подходить к слову B.\n"
        "- Не смешивай слова между собой и не пропускай поле word.\n"
        "- Каждая hints содержит ровно 3 непустые подсказки.\n"
        "- Не называй само слово и не используй однокоренные слова."
    )

    lease = await acquire_background_slot("hint_generation_batch", "opencode_go", model_name)
    if lease is None:
        return {}
    try:
        async with lease:
            response_text, _ = await get_provider_router().get_response(
                preferred_model=model_name,
                history=[{"role": "user", "parts": [prompt]}],
                system_instruction=None,
                use_openrouter=False,
                max_key_retries=1,
                thinking_level="off",
                timeout=25.0,
            )
    except Exception as exc:
        await record_result("opencode_go", model_name, "transient", reason=str(exc)[:500])
        logger.debug("Batch hint prewarm failed category=%r words=%r: %s", category, words, exc)
        return {}

    if is_error_message(response_text):
        await record_result(
            "opencode_go",
            model_name,
            classify_key_error(response_text),
            retry_after_seconds=extract_retry_after_seconds(response_text),
            reason=strip_error_tag(response_text)[:500],
        )
        return {}

    accepted = _extract_batched_hints(response_text or "", words)
    if accepted:
        await record_result("opencode_go", model_name, "success")
    return accepted


async def _prewarm_topic_hints(words: tuple[str, ...], category: str, *, topic_id: str = "") -> None:
    from app.games.judge import generate_hints
    from app.games.judgement_cache import cache_hints, get_cached_hints

    pending_words: list[str] = []
    for word in words:
        cached = await get_cached_hints(word, category, topic_id=topic_id)
        if cached:
            continue
        pending_words.append(word)
    if not pending_words:
        return

    batch_hits = await _generate_batched_hints(tuple(pending_words), category)
    for word in pending_words:
        normalized_word = _normalize_batch_word(word)
        hints = batch_hits.get(normalized_word)
        if hints:
            await cache_hints(word, category, hints, topic_id=topic_id)
            continue
        fallback_hints = await generate_hints(word, category, mode="background")
        if fallback_hints:
            await cache_hints(word, category, fallback_hints, topic_id=topic_id)


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

    if mode == "background" and should_pause_background_prefetch():
        return None

    key = _hint_key(word, category, topic_id)
    inflight = _HINTS_INFLIGHT.get(key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    async def _do_generate() -> list[str] | None:
        if mode == "background" and should_pause_background_prefetch():
            return None
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


async def enqueue_bank_hint_prewarm(words: list[str], category: str, *, topic_id: str = "") -> bool:
    global _BANK_PREFETCH_WORKER

    if not await is_hint_prewarm_enabled():
        logger.info("Skipping bank hint prewarm for category=%r because runtime switch is off", category)
        return False

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


async def get_hint_prewarm_health() -> dict[str, int | bool]:
    return {
        "enabled": await is_hint_prewarm_enabled(),
        "queue_depth": len(_BANK_PREFETCH_QUEUE),
        "pending_topics": len(_BANK_PREFETCH_PENDING_TOPICS),
        "inflight_hints": len(_HINTS_INFLIGHT),
        "worker_running": _BANK_PREFETCH_WORKER is not None and not _BANK_PREFETCH_WORKER.done(),
    }


async def _run_bank_prefetch_worker() -> None:
    global _BANK_PREFETCH_WORKER

    try:
        while _BANK_PREFETCH_QUEUE:
            if should_pause_background_prefetch():
                await asyncio.sleep(0.5)
                continue

            item = _BANK_PREFETCH_QUEUE.popleft()
            _BANK_PREFETCH_PENDING_TOPICS.discard(item.topic_key)
            try:
                await _prewarm_topic_hints(item.words, item.category, topic_id=item.topic_id)
            except Exception as exc:
                logger.debug(
                    "Bank hint prewarm failed words=%r category=%r topic_id=%r: %s",
                    item.words,
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
