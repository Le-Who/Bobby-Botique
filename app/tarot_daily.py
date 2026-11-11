from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app import database as db
from app.errors import (
    ErrorCode,
    classify_error_from_exception,
    extract_error_code,
    extract_retry_after_seconds,
    is_error_message,
    tag_error,
)
from app.games.daily_execution import checkpoint
from app.process_policies import ResolvedPolicy, resolve_process, validate_policy
from app.prompt_registry import get_prompt_text, register_controlled_text
from app.providers import get_provider_router
from app.repos.keys import count_gemini_keys
from app.repos.tarot_generation import (
    GenerationTicket,
    begin_generation,
    finish_generation,
    lock_generation,
    transaction,
)
from app.runtime_settings import interactive_prompts as _interactive_prompts  # noqa: F401
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.tarot import iter_daily_card_variants
from app.utils.time import get_pacific_tz

logger = logging.getLogger(__name__)

TAROT_DAILY_MODEL = "gemini-3.5-flash-lite"
TAROT_DAILY_RPM = 15
TAROT_DAILY_REQUEST_INTERVAL_SECONDS = 60.0 / TAROT_DAILY_RPM
TAROT_DAILY_MAX_KEY_RETRIES = 4
TAROT_DAILY_RETRY_DELAYS = (15.0, 30.0)
TAROT_DAILY_LANGUAGE = "ru"
TAROT_DAILY_SYSTEM_PROMPT = (
    "Ты — мистический таролог. Ты заранее готовишь универсальный текст для карты дня.\n"
    "Не упоминай, что текст сгенерирован заранее. Не обращайся к пользователю по имени.\n"
    "Используй значение карты ниже и дай:\n"
    "1. Краткое описание энергии дня (2-3 предложения)\n"
    "2. Практический совет на сегодня (1-2 предложения)\n"
    "3. От чего стоит остеречься (1 предложение)\n\n"
    "Ответ короткий, 6-8 предложений, Markdown, русский язык.\n"
    "---\nКАРТА ДНЯ:\n{tarot_context}\n---"
)
register_controlled_text("tarot.daily", TAROT_DAILY_SYSTEM_PROMPT, "Таро: карта дня")
_PREPARATION_WINDOW_HOURS_PT = {22, 23}
_LABEL_RE = re.compile(r"^(?P<card>.+?)\s+\((?P<orientation>Прямая|Перевернутая)\)$")
_STOP_BATCH_ERROR_CODES = frozenset(
    {
        ErrorCode.KEYS_EXHAUSTED,
        ErrorCode.QUOTA_EXCEEDED,
        ErrorCode.RATE_LIMIT,
        ErrorCode.INVALID_KEY,
        ErrorCode.DECRYPTION_FAILED,
        ErrorCode.NO_KEYS,
    }
)
_TRANSIENT_ERROR_CODES = frozenset({ErrorCode.TIMEOUT, ErrorCode.OVERLOADED, ErrorCode.NETWORK})

_local_locks: weakref.WeakValueDictionary[tuple[date, str], asyncio.Lock] = weakref.WeakValueDictionary()


@dataclass(frozen=True)
class TarotDailyReading:
    reading_date: date
    card_name: str
    orientation: str
    language: str
    body_markdown: str
    model_name: str


@dataclass(frozen=True)
class TarotDailyPreparationResult:
    target_date: date
    generated: int = 0
    skipped: int = 0
    failed: int = 0
    locked: bool = False
    remaining: int = 0


async def resolve_daily_model_policy(model: str | None = None) -> ResolvedPolicy:
    """An explicit operation model never inherits an administrator's process chain."""
    if model is None:
        return await resolve_process("tarot.daily", (TAROT_DAILY_MODEL,))
    from app.repos.models_repo import get_models

    if not isinstance(model, str) or model not in await get_models("gemini"):
        raise ValueError("invalid Tarot model")
    validate_policy("tarot.daily", {"models": [model], "strategy": "sequential"})
    return ResolvedPolicy((model,), strategy="sequential", explicit=True)


def today_reading_date(now: datetime | None = None) -> date:
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(get_pacific_tz()).date()


def is_preparation_window(now: datetime | None = None) -> bool:
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(get_pacific_tz()).hour in _PREPARATION_WINDOW_HOURS_PT


def parse_card_label(label: str) -> tuple[str, str] | None:
    match = _LABEL_RE.match(label.strip())
    if not match:
        return None
    return match.group("card"), match.group("orientation")


async def get_prepared_daily_reading(
    *,
    reading_date: date,
    card_name: str,
    orientation: str,
    language: str = TAROT_DAILY_LANGUAGE,
) -> TarotDailyReading | None:
    rows = await db.db_query(
        """
        SELECT reading_date, card_name, orientation, language, body_markdown, model_name
        FROM public.tarot_daily_readings
        WHERE reading_date = $1
          AND card_name = $2
          AND orientation = $3
          AND language = $4
        """,
        (reading_date, card_name, orientation, language),
    )
    if not rows:
        return None
    return _reading_from_row(rows[0])


async def get_prepared_daily_readings(
    *, reading_date: date, language: str = TAROT_DAILY_LANGUAGE
) -> list[TarotDailyReading]:
    """Read the day's prepared variants once for scheduled delivery admission."""
    rows = await db.db_query(
        """
        SELECT reading_date, card_name, orientation, language, body_markdown, model_name
        FROM public.tarot_daily_readings
        WHERE reading_date = $1 AND language = $2
        """,
        (reading_date, language),
    )
    return [_reading_from_row(row) for row in rows]


async def upsert_prepared_daily_reading(
    *,
    reading_date: date,
    card_name: str,
    orientation: str,
    language: str,
    body_markdown: str,
    model_name: str = TAROT_DAILY_MODEL,
    ticket: GenerationTicket | None = None,
    conn=None,
) -> None:
    await checkpoint()
    async with transaction(conn) as acquired:
        if not await lock_generation(acquired, reading_date, language, ticket):
            return
        conflict = (
            """DO UPDATE SET body_markdown=EXCLUDED.body_markdown,
                        model_name=EXCLUDED.model_name,generated_at=EXCLUDED.generated_at"""
            if ticket
            else "DO NOTHING"
        )
        await acquired.execute(
            """
        INSERT INTO public.tarot_daily_readings
            (reading_date, card_name, orientation, language, body_markdown, model_name, generated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (reading_date, card_name, orientation, language)
        """
            + conflict,
            reading_date,
            card_name,
            orientation,
            language,
            body_markdown.strip(),
            model_name,
        )


async def count_prepared_daily_readings(
    target_date: date,
    *,
    language: str = TAROT_DAILY_LANGUAGE,
) -> int:
    rows = await db.db_query(
        """
        SELECT COUNT(*) AS cnt
        FROM public.tarot_daily_readings
        WHERE reading_date = $1 AND language = $2
        """,
        (target_date, language),
    )
    return int(rows[0]["cnt"]) if rows else 0


@runtime_settings_scope()
async def prepare_daily_readings(
    *,
    target_date: date | None = None,
    language: str = TAROT_DAILY_LANGUAGE,
    force: bool = False,
    model: str | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    progress: Callable[..., Awaitable[None]] | None = None,
) -> TarotDailyPreparationResult:
    target = target_date or today_reading_date()
    if language != TAROT_DAILY_LANGUAGE:
        raise ValueError("unsupported Tarot language")
    policy = await resolve_daily_model_policy(model)
    lock = _local_locks.setdefault((target, language), asyncio.Lock())
    if lock.locked():
        return TarotDailyPreparationResult(target_date=target, locked=True)

    async with lock:
        redis_lock = await _acquire_redis_lock(target, language)
        if redis_lock is False:
            return TarotDailyPreparationResult(target_date=target, locked=True)

        generated = 0
        skipped = 0
        failed = 0
        ticket = None
        try:
            variants = list(iter_daily_card_variants())
            ticket = await begin_generation(target, language)
            if progress:
                await progress(generated=0, skipped=0, failed=0, remaining=len(variants))
            router = get_provider_router()

            # Keep a bounded per-card retry budget. Tarot prep is background
            # work; it must not be able to burn through the whole Gemini pool
            # and suspend every key used by interactive inline/chat requests.
            pool_size = await count_gemini_keys()
            max_key_retries = (
                min(pool_size, TAROT_DAILY_MAX_KEY_RETRIES) if pool_size > 0 else TAROT_DAILY_MAX_KEY_RETRIES
            )
            logger.info(
                "Tarot prep: using max_key_retries=%d (pool_size=%d)",
                max_key_retries,
                pool_size,
            )

            _consecutive_failures = 0
            _CONSECUTIVE_FAILURE_THRESHOLD = 3
            _FAILURE_BACKOFF_SECONDS = 30.0

            for index, variant in enumerate(variants):
                await checkpoint()
                requested_generation = False
                card_name = str(variant["name"])
                orientation = str(variant["orientation"])
                if not force:
                    existing = await get_prepared_daily_reading(
                        reading_date=target,
                        card_name=card_name,
                        orientation=orientation,
                        language=language,
                    )
                    if existing:
                        skipped += 1
                        if progress:
                            await progress(
                                generated=generated,
                                skipped=skipped,
                                failed=failed,
                                remaining=len(variants) - generated - skipped - failed,
                            )
                        continue

                requested_generation = True
                history = [
                    {
                        "role": "user",
                        "parts": [
                            (
                                "Подготовь универсальную карту дня без обращения к конкретному пользователю. "
                                f"Дата: {target.isoformat()}. Карта: {variant['label']}."
                            )
                        ],
                    }
                ]
                instruction = _build_daily_system_instruction(str(variant["context"]))
                baseline_model = policy.models[0]
                successful_model = baseline_model

                def _record_model(model_name: str) -> None:
                    nonlocal successful_model
                    successful_model = model_name

                result = await _generate_card_with_recovery(
                    router=router,
                    policy=policy,
                    history=history,
                    instruction=instruction,
                    max_key_retries=max_key_retries,
                    on_model_used=_record_model,
                    sleep=sleep,
                )
                if not result or not result.strip() or is_error_message(result):
                    failed += 1
                    _consecutive_failures += 1
                    logger.warning("Prepared tarot daily failed date=%s card=%s", target, variant["label"])
                    if _should_stop_batch_after_error(result):
                        logger.warning(
                            "Tarot prep: stopping batch after key-pool error date=%s card=%s",
                            target,
                            variant["label"],
                        )
                        if progress:
                            await progress(
                                generated=generated,
                                skipped=skipped,
                                failed=failed,
                                remaining=len(variants) - generated - skipped - failed,
                            )
                        break
                    # Adaptive backoff: if the API is in a sustained overload storm,
                    # pause long enough for 15s-suspended keys to recover before
                    # the next card tries to acquire a key from the pool again.
                    if _consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
                        logger.warning(
                            "Tarot prep: %d consecutive failures — pausing %.0fs for API recovery",
                            _consecutive_failures,
                            _FAILURE_BACKOFF_SECONDS,
                        )
                        await sleep(_FAILURE_BACKOFF_SECONDS)
                        _consecutive_failures = 0
                else:
                    _consecutive_failures = 0
                    await upsert_prepared_daily_reading(
                        reading_date=target,
                        card_name=card_name,
                        orientation=orientation,
                        language=language,
                        body_markdown=result.strip(),
                        model_name=successful_model,
                        ticket=ticket,
                    )
                    generated += 1
                    if progress:
                        await progress(actual_model=successful_model)

                if progress:
                    await progress(
                        generated=generated,
                        skipped=skipped,
                        failed=failed,
                        remaining=len(variants) - generated - skipped - failed,
                    )

                if requested_generation and index < len(variants) - 1:
                    await sleep(TAROT_DAILY_REQUEST_INTERVAL_SECONDS)
        finally:
            try:
                if ticket is not None:
                    await finish_generation(ticket)
            finally:
                await _release_redis_lock(redis_lock)

        return TarotDailyPreparationResult(
            target_date=target,
            generated=generated,
            skipped=skipped,
            failed=failed,
            remaining=len(variants) - generated - skipped - failed,
        )


async def _generate_card_with_recovery(
    *,
    router,
    policy: ResolvedPolicy,
    history: list,
    instruction: str,
    max_key_retries: int,
    on_model_used: Callable[[str], None],
    sleep: Callable[[float], Awaitable[object]],
) -> str:
    """Retry one missing interpretation; persistence stays outside recovery."""
    last_transient: str | None = None
    last_retry_after: int | None = None
    for attempt in range(len(TAROT_DAILY_RETRY_DELAYS) + 1):
        await checkpoint()
        temporarily_unavailable = False
        retry_after = None
        try:
            if policy.explicit:
                result = await router.execute_gemini_model_plan(
                    policy.models,
                    history,
                    parse_response=lambda text: text,
                    system_instruction=instruction,
                    max_keys=max_key_retries,
                    thinking_level="low",
                    timeout=45,
                    on_model_used=on_model_used,
                )
            else:
                result, _tokens = await router.get_response(
                    preferred_model=policy.models[0],
                    history=history,
                    system_instruction=instruction,
                    use_openrouter=False,
                    max_key_retries=max_key_retries,
                    thinking_level="low",
                    timeout=45,
                    on_model_used=on_model_used,
                    preserve_transient_errors=True,
                )
        except Exception as exc:
            temporarily_unavailable = (
                policy.explicit and str(exc) == "No Gemini keys available for the requested model plan"
            )
            code = (
                ErrorCode.KEYS_EXHAUSTED
                if temporarily_unavailable
                else extract_error_code(str(exc)) or classify_error_from_exception(exc)
            )
            if not policy.explicit and code not in _TRANSIENT_ERROR_CODES:
                raise
            retry_after = extract_retry_after_seconds(str(exc))
            result = tag_error(code, "Prepared tarot daily provider unavailable")

        result_code = extract_error_code(result or "")
        if result_code in _TRANSIENT_ERROR_CODES:
            last_transient = result
            last_retry_after = retry_after or extract_retry_after_seconds(result)
        elif last_transient is not None and (
            temporarily_unavailable or (not policy.explicit and result_code == ErrorCode.KEYS_EXHAUSTED)
        ):
            # A transient failure can briefly suspend the whole model pool.
            # Opt-in routing preserves actual quota/configuration failures,
            # leaving KEYS_EXHAUSTED for selection without a provider request.
            result = last_transient
        else:
            return result
        if attempt == len(TAROT_DAILY_RETRY_DELAYS):
            return result
        delay = max(TAROT_DAILY_RETRY_DELAYS[attempt], last_retry_after or 0)
        logger.warning(
            "Tarot prep: transient card generation failure; retry %d in %.0fs",
            attempt + 1,
            delay,
        )
        await sleep(delay)
    raise AssertionError("unreachable")


def _should_stop_batch_after_error(text: str | None) -> bool:
    code = extract_error_code(text or "")
    return code in _STOP_BATCH_ERROR_CODES


def _build_daily_system_instruction(tarot_context: str) -> str:
    return (
        get_prompt_text("tarot.scope")
        + "\n\n"
        + get_prompt_text("tarot.daily").replace("{tarot_context}", tarot_context)
    )


def _reading_from_row(row) -> TarotDailyReading:
    row_date = row["reading_date"]
    if isinstance(row_date, datetime):
        row_date = row_date.date()
    return TarotDailyReading(
        reading_date=row_date,
        card_name=row["card_name"],
        orientation=row["orientation"],
        language=row["language"],
        body_markdown=row["body_markdown"],
        model_name=row["model_name"],
    )


async def _acquire_redis_lock(target_date: date, language: str):
    try:
        from app.cache import redis_client

        if not redis_client:
            return None
        lock = redis_client.lock(
            f"tarot:daily:prep:{target_date.isoformat()}:{language}",
            timeout=1800,
            blocking_timeout=1,
        )
        acquired = await lock.acquire()
        return lock if acquired else False
    except Exception as exc:
        logger.debug("Tarot daily Redis lock unavailable: %s", exc)
        return None


async def _release_redis_lock(lock) -> None:
    if not lock:
        return
    with contextlib.suppress(Exception):
        await lock.release()
