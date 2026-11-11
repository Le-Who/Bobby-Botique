"""Tarot Daily Subscriptions repository.

Stores per-user preferences for the "Карта дня" (daily tarot card) broadcast:
  - is_subscribed:        global on/off toggle
  - timezone:             IANA tz string for local-time scheduling
  - preferred_local_hour: preferred delivery hour (0-23) in user's local TZ
  - last_sent_date:       last date card was delivered (prevents same-day re-send)

``get_due_for_current_hour`` resolves each active subscriber's IANA timezone,
allows same-day catch-up after the preferred hour, and excludes dates already
delivered. The broad date query also includes subscribers whose local day is
ahead of UTC.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.database import db_query

logger = logging.getLogger(__name__)

TABLE = "public.tarot_daily_subscriptions"
DELIVERY_SETTING_KEY = "tarot_daily_delivery_enabled"


@dataclass(frozen=True, slots=True)
class TarotDeliverySlot:
    slot_date: date
    subscription_fingerprint: str
    reason_code: str | None


def resolve_delivery_slot(subscription: Mapping[str, Any], now: datetime) -> TarotDeliverySlot:
    """Resolve a local delivery date and schedule identity without side effects."""
    timezone = subscription.get("timezone")
    hour = subscription.get("preferred_local_hour")
    if not isinstance(timezone, str) or not timezone or type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("invalid_tarot_schedule")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid_tarot_schedule") from exc
    last_sent = subscription.get("last_sent_date")
    if last_sent is not None and type(last_sent) is not date:
        raise ValueError("invalid_tarot_schedule")
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local_now = current.astimezone(zone)
    slot_date = local_now.date()
    reason = None
    if last_sent is not None and last_sent >= slot_date:
        reason = "already_delivered"
    elif local_now.hour < hour:
        reason = "slot_not_due"
    fingerprint = hashlib.sha256(f"{timezone}:{hour}".encode()).hexdigest()
    return TarotDeliverySlot(slot_date, fingerprint, reason)


# ── Read ──────────────────────────────────────────────────────────────────────


async def get_tarot_subscription(user_id: int) -> dict | None:
    """Return the subscription row for a user, or None if not found."""
    rows = await db_query(
        f"""
        SELECT user_id, is_subscribed, timezone, preferred_local_hour,
               last_sent_date, discovery_last_sent_at, created_at, updated_at
        FROM {TABLE}
        WHERE user_id = $1
        """,
        (user_id,),
    )
    return dict(rows[0]) if rows else None


async def count_active_subscribers() -> int:
    """Return total number of active tarot daily subscribers."""
    rows = await db_query(f"SELECT COUNT(*) AS cnt FROM {TABLE} WHERE is_subscribed = TRUE")
    return int(rows[0]["cnt"]) if rows else 0


async def get_due_tarot_subscriptions(today: date) -> list[dict]:
    """Return subscribers who haven't received their card today.

    Used by the delivery scheduler. Returns all active subscribers whose
    last_sent_date is before today (or NULL).
    """
    rows = await db_query(
        f"""
        SELECT user_id, timezone, preferred_local_hour, last_sent_date
        FROM {TABLE}
        WHERE is_subscribed = TRUE
          AND (last_sent_date IS NULL OR last_sent_date < $1)
        ORDER BY preferred_local_hour, user_id
        """,
        (today,),
    )
    return [dict(r) for r in rows]


# ── Write ─────────────────────────────────────────────────────────────────────


async def upsert_tarot_subscription(
    user_id: int,
    *,
    is_subscribed: bool | None = None,
    timezone: str | None = None,
    preferred_local_hour: int | None = None,
) -> bool:
    """Create or partially update a user's tarot daily subscription.

    Only the provided (non-None) values are written. Calling with
    ``is_subscribed=True`` and no other args creates the row with defaults if
    it doesn't exist yet.
    """
    insert_columns = ["user_id"]
    update_sets: list[str] = []
    params: list = [user_id]

    if is_subscribed is not None:
        insert_columns.append("is_subscribed")
        update_sets.append("is_subscribed = EXCLUDED.is_subscribed")
        params.append(is_subscribed)

    if timezone is not None:
        insert_columns.append("timezone")
        update_sets.append("timezone = EXCLUDED.timezone")
        params.append(timezone)

    if preferred_local_hour is not None:
        insert_columns.append("preferred_local_hour")
        update_sets.append("preferred_local_hour = EXCLUDED.preferred_local_hour")
        params.append(preferred_local_hour)

    try:
        columns_sql = ", ".join(insert_columns)
        values_sql = ", ".join(f"${index}" for index in range(1, len(params) + 1))
        if not update_sets:
            # No preferences supplied — just ensure the row exists.
            await db_query(
                f"""
                INSERT INTO {TABLE} (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id,),
            )
        else:
            set_clause = ", ".join([*update_sets, "updated_at = NOW()"])
            await db_query(
                f"""
                INSERT INTO {TABLE} ({columns_sql})
                VALUES ({values_sql})
                ON CONFLICT (user_id) DO UPDATE SET {set_clause}
                """,
                tuple(params),
            )
        return True
    except Exception as e:
        logger.error(
            "upsert_tarot_subscription failed for user %s: %s",
            user_id,
            e,
            exc_info=True,
        )
        return False


async def mark_tarot_sent(user_id: int, sent_date: date) -> None:
    """Record that a user received their tarot card on sent_date."""
    await db_query(
        f"""
        UPDATE {TABLE}
        SET last_sent_date = $2, updated_at = NOW()
        WHERE user_id = $1
        """,
        (user_id, sent_date),
    )


async def unsubscribe_tarot(user_id: int) -> bool:
    """Unsubscribe a user. Returns True if the row existed."""
    rows = await db_query(
        f"""
        UPDATE {TABLE}
        SET is_subscribed = FALSE, updated_at = NOW()
        WHERE user_id = $1
        RETURNING user_id
        """,
        (user_id,),
    )
    return bool(rows)


# ── Delivery scheduler helper ─────────────────────────────────────────────────


async def get_due_for_current_hour(now: datetime | None = None) -> list[dict]:
    """Return subscribers due on their local day, including same-day catch-up."""
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    # UTC+14 can already be on tomorrow's date. Filter the broad pull against
    # each subscriber's actual local date before admitting a broadcast snapshot.
    latest_local_date = (current.astimezone(UTC) + timedelta(days=1)).date()
    due = []
    for subscription in await get_due_tarot_subscriptions(latest_local_date):
        try:
            slot = resolve_delivery_slot(subscription, current)
        except ValueError:
            logger.warning("Tarot delivery schedule invalid for user_id=%s", subscription.get("user_id"))
            continue
        if slot.reason_code is None:
            due.append({**subscription, "slot_date": slot.slot_date})
    return due


async def mark_tarot_discovery_sent(user_id: int) -> None:
    """Record that the user received a tarot subscription offer right now."""
    try:
        await db_query(
            f"""
            INSERT INTO {TABLE} (user_id, discovery_last_sent_at)
            VALUES ($1, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
                SET discovery_last_sent_at = CURRENT_TIMESTAMP,
                    updated_at = NOW()
            """,
            (user_id,),
        )
    except Exception as e:
        logger.error("mark_tarot_discovery_sent failed for user %s: %s", user_id, e)


async def get_tarot_discovery_candidates(
    *,
    min_days_since_last_offer: int = 14,
    limit: int = 200,
) -> list[dict]:
    """Return users who should receive a tarot subscription offer.

    Candidates = users with is_authorized=1 who are not yet subscribed AND
    either never received an offer OR received one 14+ days ago.
    """
    from app.database import db_query as _db_query

    try:
        rows = await _db_query(
            f"""
            SELECT u.user_id,
                   ts.is_subscribed,
                   ts.discovery_last_sent_at
            FROM public.users u
            LEFT JOIN {TABLE} ts ON ts.user_id = u.user_id
            WHERE u.is_authorized = 1
              AND COALESCE(ts.is_subscribed, FALSE) = FALSE
              AND (
                  ts.discovery_last_sent_at IS NULL
                  OR ts.discovery_last_sent_at <= NOW() - ($1 || ' days')::INTERVAL
              )
            ORDER BY u.user_id
            LIMIT $2
            """,
            (min_days_since_last_offer, limit),
        )
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.error("get_tarot_discovery_candidates failed: %s", e, exc_info=True)
        return []
