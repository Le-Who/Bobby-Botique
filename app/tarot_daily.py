from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app import database as db
from app.errors import is_error_message
from app.providers import get_provider_router
from app.tarot import iter_daily_card_variants
from app.utils.time import get_pacific_tz

logger = logging.getLogger(__name__)

TAROT_DAILY_MODEL = "gemini-3.1-flash-lite"
TAROT_DAILY_RPM = 15
TAROT_DAILY_REQUEST_INTERVAL_SECONDS = 60.0 / TAROT_DAILY_RPM
TAROT_DAILY_LANGUAGE = "ru"
_PREPARATION_WINDOW_HOURS_PT = {22, 23}
_LABEL_RE = re.compile(r"^(?P<card>.+?)\s+\((?P<orientation>Прямая|Перевернутая)\)$")

_local_locks: dict[tuple[date, str], asyncio.Lock] = {}


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


async def upsert_prepared_daily_reading(
    *,
    reading_date: date,
    card_name: str,
    orientation: str,
    language: str,
    body_markdown: str,
    model_name: str = TAROT_DAILY_MODEL,
) -> None:
    await db.db_query(
        """
        INSERT INTO public.tarot_daily_readings
            (reading_date, card_name, orientation, language, body_markdown, model_name, generated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (reading_date, card_name, orientation, language)
        DO UPDATE SET
            body_markdown = EXCLUDED.body_markdown,
            model_name = EXCLUDED.model_name,
            generated_at = EXCLUDED.generated_at
        """,
        (reading_date, card_name, orientation, language, body_markdown.strip(), model_name),
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


async def prepare_daily_readings(
    *,
    target_date: date | None = None,
    language: str = TAROT_DAILY_LANGUAGE,
    force: bool = False,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> TarotDailyPreparationResult:
    target = target_date or today_reading_date()
    lock = _local_locks.setdefault((target, language), asyncio.Lock())

    async with lock:
        redis_lock = await _acquire_redis_lock(target, language)
        if redis_lock is False:
            return TarotDailyPreparationResult(target_date=target, locked=True)

        generated = 0
        skipped = 0
        failed = 0
        try:
            variants = list(iter_daily_card_variants())
            router = get_provider_router()
            for index, variant in enumerate(variants):
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
                        continue

                requested_generation = True
                result, _tokens = await router.get_response(
                    preferred_model=TAROT_DAILY_MODEL,
                    history=[
                        {
                            "role": "user",
                            "parts": [
                                (
                                    "Подготовь универсальную карту дня без обращения к конкретному пользователю. "
                                    f"Дата: {target.isoformat()}. Карта: {variant['label']}."
                                )
                            ],
                        }
                    ],
                    system_instruction=_build_daily_system_instruction(str(variant["context"])),
                    use_openrouter=False,
                    max_key_retries=4,
                    thinking_level="low",
                    timeout=45,
                )
                if not result or not result.strip() or is_error_message(result):
                    failed += 1
                    logger.warning("Prepared tarot daily failed date=%s card=%s", target, variant["label"])
                else:
                    await upsert_prepared_daily_reading(
                        reading_date=target,
                        card_name=card_name,
                        orientation=orientation,
                        language=language,
                        body_markdown=result.strip(),
                        model_name=TAROT_DAILY_MODEL,
                    )
                    generated += 1

                if requested_generation and index < len(variants) - 1:
                    await sleep(TAROT_DAILY_REQUEST_INTERVAL_SECONDS)
        finally:
            await _release_redis_lock(redis_lock)

        return TarotDailyPreparationResult(
            target_date=target,
            generated=generated,
            skipped=skipped,
            failed=failed,
        )


def _build_daily_system_instruction(tarot_context: str) -> str:
    return (
        "Ты — мистический таролог. Ты заранее готовишь универсальный текст для карты дня.\n"
        "Не упоминай, что текст сгенерирован заранее. Не обращайся к пользователю по имени.\n"
        "Используй значение карты ниже и дай:\n"
        "1. Краткое описание энергии дня (2-3 предложения)\n"
        "2. Практический совет на сегодня (1-2 предложения)\n"
        "3. От чего стоит остеречься (1 предложение)\n\n"
        "Ответ короткий, 6-8 предложений, Markdown, русский язык.\n"
        f"---\nКАРТА ДНЯ:\n{tarot_context}\n---"
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
