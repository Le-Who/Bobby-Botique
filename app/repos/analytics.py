"""
User analytics repository — engagement tracking, streaks, and retention.

Provides:
- record_daily_activity() — bump streak on each active day
- get_user_streak() — current and longest streak for a user
- get_dau_count() — daily active users count
- get_retention_stats() — D1/D7/D30 retention rates
- generate_auto_title() — AI-generated conversation title from first messages
"""

import logging
from datetime import date, timedelta
from typing import Any

import asyncpg

from app import database as db

# ─── Streak Tracking ────────────────────────────────────────────────────────


async def record_daily_activity(user_id: int) -> dict[str, int]:
    """Increment the user's streak if they haven't been active today yet.

    Uses a single upsert + conditional streak logic:
    - If the user was active yesterday, increment current_streak.
    - If the user was active today already, do nothing.
    - Otherwise, reset current_streak to 1.

    Returns dict with current_streak and longest_streak.
    """
    try:
        today = date.today()
        yesterday = today - timedelta(days=1)

        # Check if user was active yesterday
        prev = await db.db_query(
            "SELECT current_streak FROM user_metrics WHERE user_id = $1 AND metric_date = $2",
            (user_id, yesterday),
        )
        prev_streak = prev[0]["current_streak"] if prev else 0

        new_streak = prev_streak + 1 if prev_streak > 0 else 1

        # Upsert today's row with streak
        result = await db.db_query(
            """
            INSERT INTO user_metrics (user_id, metric_date, request_count, current_streak, longest_streak)
            VALUES ($1, $2, 0, $3, $3)
            ON CONFLICT (user_id, metric_date) DO UPDATE SET
                current_streak = CASE
                    WHEN user_metrics.current_streak = 0 THEN $3
                    ELSE user_metrics.current_streak
                END,
                longest_streak = GREATEST(user_metrics.longest_streak, $3)
            RETURNING current_streak, longest_streak
            """,
            (user_id, today, new_streak),
        )

        if result:
            return {
                "current_streak": result[0]["current_streak"],
                "longest_streak": result[0]["longest_streak"],
            }
        return {"current_streak": 0, "longest_streak": 0}

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to record daily activity for user %s: %s", user_id, e)
        return {"current_streak": 0, "longest_streak": 0}


async def get_user_streak(user_id: int) -> dict[str, int]:
    """Get the current and longest streak for a user."""
    try:
        result = await db.db_query(
            "SELECT current_streak, longest_streak FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
            (user_id,),
        )
        if result:
            return {
                "current_streak": result[0]["current_streak"] or 0,
                "longest_streak": result[0]["longest_streak"] or 0,
            }
        return {"current_streak": 0, "longest_streak": 0}
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to get streak for user %s: %s", user_id, e)
        return {"current_streak": 0, "longest_streak": 0}


# ─── DAU / Retention ────────────────────────────────────────────────────────


async def get_dau_count(target_date: date | None = None) -> int:
    """Count distinct users active on a given date (default: today)."""
    d = target_date or date.today()
    try:
        result = await db.db_query(
            "SELECT COUNT(DISTINCT user_id) AS cnt FROM user_metrics WHERE metric_date = $1 AND request_count > 0",
            (d,),
        )
        return result[0]["cnt"] if result else 0
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to get DAU for %s: %s", d, e)
        return 0


async def get_retention_stats() -> dict[str, Any]:
    """Calculate D1, D7, and D30 retention rates.

    Retention = users active on Day-N who were also active on Day-0 (registration day).
    Uses a simplified approach: compares today's actives vs. actives N days ago.
    """
    today = date.today()
    try:
        result = await db.db_query(
            """
            WITH
                d1_users AS (
                    SELECT DISTINCT user_id FROM user_metrics
                    WHERE metric_date = $1 AND request_count > 0
                ),
                d7_users AS (
                    SELECT DISTINCT user_id FROM user_metrics
                    WHERE metric_date = $2 AND request_count > 0
                ),
                d30_users AS (
                    SELECT DISTINCT user_id FROM user_metrics
                    WHERE metric_date = $3 AND request_count > 0
                ),
                today_users AS (
                    SELECT DISTINCT user_id FROM user_metrics
                    WHERE metric_date = $4 AND request_count > 0
                )
            SELECT
                (SELECT COUNT(*) FROM today_users) AS dau,
                (SELECT COUNT(*) FROM d1_users d WHERE d.user_id IN (SELECT user_id FROM today_users)) AS retained_d1,
                (SELECT COUNT(*) FROM d1_users) AS total_d1,
                (SELECT COUNT(*) FROM d7_users d WHERE d.user_id IN (SELECT user_id FROM today_users)) AS retained_d7,
                (SELECT COUNT(*) FROM d7_users) AS total_d7,
                (SELECT COUNT(*) FROM d30_users d WHERE d.user_id IN (SELECT user_id FROM today_users)) AS retained_d30,
                (SELECT COUNT(*) FROM d30_users) AS total_d30
            """,
            (
                today - timedelta(days=1),
                today - timedelta(days=7),
                today - timedelta(days=30),
                today,
            ),
        )

        if not result:
            return {"dau": 0, "d1": 0.0, "d7": 0.0, "d30": 0.0}

        row = result[0]
        dau = row["dau"]

        def safe_rate(retained: int, total: int) -> float:
            return round((retained / total) * 100, 1) if total > 0 else 0.0

        return {
            "dau": dau,
            "d1": safe_rate(row["retained_d1"], row["total_d1"]),
            "d7": safe_rate(row["retained_d7"], row["total_d7"]),
            "d30": safe_rate(row["retained_d30"], row["total_d30"]),
        }
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to get retention stats: %s", e)
        return {"dau": 0, "d1": 0.0, "d7": 0.0, "d30": 0.0}


# ─── Engagement Stats ────────────────────────────────────────────────────────


async def get_engagement_summary(user_id: int) -> dict[str, Any]:
    """Get engagement summary for a user over the last 7 days."""
    try:
        result = await db.db_query(
            """
            SELECT
                COALESCE(SUM(request_count), 0) AS total_requests,
                COUNT(*) AS active_days,
                COALESCE(MAX(current_streak), 0) AS current_streak,
                COALESCE(MAX(longest_streak), 0) AS longest_streak
            FROM user_metrics
            WHERE user_id = $1
                AND metric_date >= CURRENT_DATE - INTERVAL '6 days'
                AND request_count > 0
            """,
            (user_id,),
        )
        if result:
            row = result[0]
            return {
                "total_requests_7d": row["total_requests"],
                "active_days_7d": row["active_days"],
                "avg_requests_per_day": round(row["total_requests"] / max(row["active_days"], 1), 1),
                "current_streak": row["current_streak"],
                "longest_streak": row["longest_streak"],
            }
        return {
            "total_requests_7d": 0,
            "active_days_7d": 0,
            "avg_requests_per_day": 0.0,
            "current_streak": 0,
            "longest_streak": 0,
        }
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to get engagement summary for user %s: %s", user_id, e)
        return {
            "total_requests_7d": 0,
            "active_days_7d": 0,
            "avg_requests_per_day": 0.0,
            "current_streak": 0,
            "longest_streak": 0,
        }


# ─── Conversation Auto-Title ────────────────────────────────────────────────


def generate_auto_title(messages: list[dict[str, Any]], max_len: int = 60) -> str:
    """Generate a concise conversation title from the first few messages.

    Uses a simple heuristic (no AI call) to avoid latency:
    1. Take the first user message
    2. Strip to first sentence or first max_len chars
    3. Clean up and capitalize

    This is deterministic — no API call needed.
    """
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            parts = msg.get("parts", [])
            if not parts:
                content = msg.get("content", "")
            else:
                # Flatten parts to text (skip images/bytes)
                text_parts = []
                for part in parts:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict) and "text" in part:
                        text_parts.append(part["text"])
                content = " ".join(text_parts)

            if not content or not content.strip():
                continue

            content = content.strip()

            # Truncate at first sentence boundary
            for sep in [".", "?", "!", "\n"]:
                idx = content.find(sep)
                if 5 < idx < max_len:
                    content = content[: idx + 1]
                    break

            if len(content) > max_len:
                content = content[: max_len - 3].rstrip() + "..."

            return content.strip()

    # Fallback
    from datetime import datetime

    return f"Беседа от {datetime.now().strftime('%d.%m.%Y %H:%M')}"


def streak_badge(streak: int) -> str:
    """Return an emoji badge based on streak length."""
    if streak >= 30:
        return "💎"
    elif streak >= 14:
        return "⭐"
    elif streak >= 7:
        return "🔥"
    elif streak >= 3:
        return "✨"
    elif streak >= 1:
        return "💚"
    return ""
