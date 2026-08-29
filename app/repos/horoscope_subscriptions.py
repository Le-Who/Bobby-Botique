"""Horoscope subscriptions repository.

Stores per-user preferences for daily horoscope delivery:
  - sign:            zodiac sign (e.g. 'aries', 'taurus')
  - time_today:      'HH:MM' for morning delivery, None = disabled
  - time_tomorrow:   'HH:MM' for evening delivery, None = disabled
  - utc_offset:      signed int, user's UTC offset (e.g. +3 for Moscow)
  - is_active:       global on/off toggle

Scheduler query:
    get_due_horoscope_subscriptions(utc_hour, utc_minute, kind)
    Returns active subs where the user's local time matches 'HH:MM'
    and we haven't yet delivered today (last_today_sent / last_tomorrow_sent < today).
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Any

from app.database import db_query

logger = logging.getLogger(__name__)

# Sentinel to distinguish "not provided" from None (which explicitly disables a slot)
_MISSING = object()


def _normalize_delivery_time(value: Any) -> time | None:
    if value is None or isinstance(value, time):
        return value
    if isinstance(value, str):
        return time.fromisoformat(value.strip())
    raise TypeError(f"delivery time must be HH:MM string, datetime.time, or None; got {type(value).__name__}")


async def upsert_horoscope_subscription(
    user_id: int,
    sign: str | None = None,
    time_today: Any = _MISSING,
    time_tomorrow: Any = _MISSING,
    utc_offset: int | None = None,
    is_active: bool | None = None,
) -> bool:
    """Create or partially update a user's horoscope subscription.

    Only the provided (non-_MISSING) values are written. This allows callers
    to update a single field (e.g. time_today only) without overwriting the rest.

    Passing ``time_today=None`` or ``time_tomorrow=None`` explicitly disables
    that delivery slot while preserving the other.
    """
    insert_sign = sign or "aries"
    insert_columns = ["user_id", "sign"]
    params: list[Any] = [user_id, insert_sign]
    update_sets: list[str] = []

    if sign is not None:
        update_sets.append("sign = EXCLUDED.sign")

    if time_today is not _MISSING:
        insert_columns.append("time_today")
        params.append(_normalize_delivery_time(time_today))
        update_sets.append("time_today = EXCLUDED.time_today")

    if time_tomorrow is not _MISSING:
        insert_columns.append("time_tomorrow")
        params.append(_normalize_delivery_time(time_tomorrow))
        update_sets.append("time_tomorrow = EXCLUDED.time_tomorrow")

    if utc_offset is not None:
        insert_columns.append("utc_offset")
        params.append(utc_offset)
        update_sets.append("utc_offset = EXCLUDED.utc_offset")

    if is_active is not None:
        insert_columns.append("is_active")
        params.append(is_active)
        update_sets.append("is_active = EXCLUDED.is_active")

    try:
        columns_sql = ", ".join(insert_columns)
        values_sql = ", ".join(f"${index}" for index in range(1, len(params) + 1))
        if not update_sets:
            # Only updated_at — just ensure row exists
            await db_query(
                f"""
                INSERT INTO horoscope_subscriptions ({columns_sql})
                VALUES ({values_sql})
                ON CONFLICT (user_id) DO NOTHING
                """,
                tuple(params),
            )
        else:
            set_clause = ", ".join([*update_sets, "updated_at = CURRENT_TIMESTAMP"])
            await db_query(
                f"""
                INSERT INTO horoscope_subscriptions ({columns_sql})
                VALUES ({values_sql})
                ON CONFLICT (user_id) DO UPDATE SET {set_clause}
                """,
                tuple(params),
            )
        return True
    except Exception as e:
        logger.error("upsert_horoscope_subscription failed for user %s: %s", user_id, e, exc_info=True)
        return False


async def get_horoscope_subscription(user_id: int) -> dict[str, Any] | None:
    """Return the subscription record for a user, or None if not found."""
    try:
        rows = await db_query(
            """
            SELECT user_id, sign, time_today, time_tomorrow, utc_offset,
                   is_active, last_today_sent, last_tomorrow_sent,
                   discovery_last_sent_at
            FROM horoscope_subscriptions
            WHERE user_id = $1
            """,
            (user_id,),
        )
        return dict(rows[0]) if rows else None
    except Exception as e:
        logger.error("get_horoscope_subscription failed for user %s: %s", user_id, e)
        return None


async def get_due_horoscope_subscriptions(
    utc_hour: int,
    utc_minute: int,
    kind: str,  # 'today' or 'tomorrow'
) -> list[dict[str, Any]]:
    """Return active subscriptions due for delivery right now.

    A subscription is 'due' when:
      - is_active = TRUE
      - time_{kind} is set (not NULL)
      - The user's local hour+minute matches their stored time_{kind}
        (we convert UTC → local via utc_offset, wrapping mod 24)
      - We haven't already sent this kind today (last_{kind}_sent < today UTC)
    """
    if kind not in ("today", "tomorrow"):
        raise ValueError(f"kind must be 'today' or 'tomorrow', got {kind!r}")

    time_col = f"time_{kind}"
    last_sent_col = f"last_{kind}_sent"

    try:
        rows = await db_query(
            f"""
            SELECT user_id, sign, {time_col} AS delivery_time, utc_offset,
                   {last_sent_col} AS last_sent
            FROM horoscope_subscriptions
            WHERE is_active = TRUE
              AND {time_col} IS NOT NULL
              AND EXTRACT(HOUR  FROM ({time_col}::time))::int = MOD(($1::int + utc_offset + 48), 24)
              AND EXTRACT(MINUTE FROM ({time_col}::time))::int = $2::int
              AND (
                  {last_sent_col} IS NULL
                  OR {last_sent_col}::date < CURRENT_DATE
              )
            """,
            (utc_hour, utc_minute),
        )
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("get_due_horoscope_subscriptions failed (kind=%s): %s", kind, e, exc_info=True)
        return []


async def mark_horoscope_sent(user_id: int, kind: str) -> None:
    """Update last_{kind}_sent to now after a successful delivery."""
    if kind not in ("today", "tomorrow"):
        raise ValueError(f"kind must be 'today' or 'tomorrow', got {kind!r}")

    col = f"last_{kind}_sent"
    try:
        await db_query(
            f"UPDATE horoscope_subscriptions SET {col} = CURRENT_TIMESTAMP WHERE user_id = $1",
            (user_id,),
        )
    except Exception as e:
        logger.error("mark_horoscope_sent failed for user %s kind=%s: %s", user_id, kind, e)


async def mark_horoscope_discovery_sent(user_id: int) -> None:
    """Record that the user received a horoscope subscription offer right now."""
    try:
        await db_query(
            """
            INSERT INTO horoscope_subscriptions (user_id, sign, discovery_last_sent_at)
            VALUES ($1, 'aries', CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
                SET discovery_last_sent_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
            """,
            (user_id,),
        )
    except Exception as e:
        logger.error("mark_horoscope_discovery_sent failed for user %s: %s", user_id, e)


async def get_horoscope_discovery_candidates(
    *,
    min_days_since_last_offer: int = 14,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return users who should receive a horoscope subscription offer.

    Candidates = users who have interacted with the bot (present in `users`
    table with is_authorized=1) but are not yet subscribed to horoscope AND
    either never received an offer OR received one 14+ days ago.
    """
    try:
        rows = await db_query(
            """
            SELECT u.user_id,
                   hs.is_active,
                   hs.discovery_last_sent_at
            FROM public.users u
            LEFT JOIN horoscope_subscriptions hs ON hs.user_id = u.user_id
            WHERE u.is_authorized = 1
              AND COALESCE(hs.is_active, FALSE) = FALSE
              AND (
                  hs.discovery_last_sent_at IS NULL
                  OR hs.discovery_last_sent_at <= NOW() - ($1 || ' days')::INTERVAL
              )
            ORDER BY u.user_id
            LIMIT $2
            """,
            (min_days_since_last_offer, limit),
        )
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("get_horoscope_discovery_candidates failed: %s", e, exc_info=True)
        return []


async def delete_horoscope_subscription(user_id: int) -> None:
    """Fully remove a user's subscription record."""
    try:
        await db_query(
            "DELETE FROM horoscope_subscriptions WHERE user_id = $1",
            (user_id,),
        )
    except Exception as e:
        logger.error("delete_horoscope_subscription failed for user %s: %s", user_id, e)
