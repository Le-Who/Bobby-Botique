"""Scheduled Intelligence Briefs — periodic AI-curated summaries.

Commands:
- /subscribe morning_brief  — opt-in to daily briefs
- /unsubscribe morning_brief — opt-out

Pipeline:
1. Query user's LTM for recent topics/interests.
2. Use Tavily to find fresh articles on those topics.
3. Summarize with Gemini into a concise 3-5 bullet brief.
4. Send via Telegram message.

Scheduling:
- Uses `python-telegram-bot` JobQueue for timing.
- Runs once per hour, checks which subscriptions need delivery.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from app import database as db
from app.i18n import t
from app.utils.json_compat import json
from app.utils.ux_improvements import tg_time_tag, wrap_in_expandable_blockquote

logger = logging.getLogger(__name__)


def parse_brief_schedule(time_str: str) -> int:
    """Parse a time string (e.g. '08:00', '8') into a valid hour integer (0-23)."""
    time_str = time_str.strip()
    if not time_str:
        raise ValueError("Time string cannot be empty")

    try:
        # Handle formats like "08:00" or "8:00"
        if ":" in time_str:
            hour_str = time_str.split(":")[0]
            hour = int(hour_str)
        # Handle plain numbers like "8"
        else:
            hour = int(time_str)

        if not (0 <= hour <= 23):
            raise ValueError(f"Hour must be between 0 and 23, got {hour}")

        return hour
    except ValueError as e:
        # Re-raise with our custom message or the inner value error if it's ours
        if "must be between" in str(e) or "cannot be empty" in str(e):
            raise
        raise ValueError(f"Invalid time format: {time_str}") from e


# ── Database operations ──────────────────────────────────────────────────


async def get_subscription(user_id: int, sub_type: str = "morning_brief") -> dict[str, Any] | None:
    """Get a user's subscription record."""
    try:
        result = await db.db_query(
            "SELECT id, is_active, timezone, preferred_hour, last_sent_at "
            "FROM brief_subscriptions WHERE user_id = $1 AND subscription_type = $2",
            (user_id, sub_type),
        )
        if result:
            row = result[0]
            return {
                "id": row["id"],
                "is_active": row["is_active"],
                "timezone": row["timezone"],
                "preferred_hour": row["preferred_hour"],
                "last_sent_at": row["last_sent_at"],
            }
        return None
    except Exception as e:
        logger.error("Error getting subscription: %s", e, exc_info=True)
        return None


async def upsert_subscription(
    user_id: int,
    sub_type: str = "morning_brief",
    is_active: bool = True,
    timezone: str = "UTC",
    preferred_hour: int = 7,
) -> bool:
    """Create or update a subscription."""
    try:
        await db.db_query(
            """
            INSERT INTO brief_subscriptions (user_id, subscription_type, is_active, timezone, preferred_hour)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id, subscription_type) DO UPDATE SET
                is_active = EXCLUDED.is_active,
                timezone = EXCLUDED.timezone,
                preferred_hour = EXCLUDED.preferred_hour
            """,
            (user_id, sub_type, is_active, timezone, preferred_hour),
        )
        return True
    except Exception as e:
        logger.error("Error upserting subscription: %s", e, exc_info=True)
        return False


async def deactivate_subscription(user_id: int, sub_type: str = "morning_brief") -> bool:
    """Deactivate a subscription."""
    try:
        await db.db_query(
            "UPDATE brief_subscriptions SET is_active = FALSE WHERE user_id = $1 AND subscription_type = $2",
            (user_id, sub_type),
        )
        return True
    except Exception as e:
        logger.error("Error deactivating subscription: %s", e, exc_info=True)
        return False


async def get_due_subscriptions(current_hour_utc: int) -> list[dict[str, Any]]:
    """Get all active subscriptions that should fire at the given UTC hour."""
    try:
        result = await db.db_query(
            """
            SELECT bs.user_id, bs.subscription_type, bs.timezone, bs.preferred_hour, bs.last_sent_at
            FROM brief_subscriptions bs
            WHERE bs.is_active = TRUE
              AND bs.preferred_hour = $1
              AND (bs.last_sent_at IS NULL OR bs.last_sent_at < CURRENT_DATE)
            """,
            (current_hour_utc,),
        )
        return (
            [
                {
                    "user_id": row["user_id"],
                    "subscription_type": row["subscription_type"],
                    "timezone": row["timezone"],
                    "preferred_hour": row["preferred_hour"],
                    "last_sent_at": row["last_sent_at"],
                }
                for row in result
            ]
            if result
            else []
        )
    except Exception as e:
        logger.error("Error getting due subscriptions: %s", e, exc_info=True)
        return []


async def mark_sent(user_id: int, sub_type: str = "morning_brief") -> None:
    """Update last_sent_at after successful delivery."""
    try:
        await db.db_query(
            "UPDATE brief_subscriptions SET last_sent_at = $1 WHERE user_id = $2 AND subscription_type = $3",
            (datetime.now(tz=UTC), user_id, sub_type),
        )
    except Exception as e:
        logger.error("Error marking subscription as sent: %s", e, exc_info=True)


# ── Brief generation pipeline ───────────────────────────────────────────


async def _get_user_topics(user_id: int) -> list[str]:
    """Extract recent discussion topics from user's LTM."""
    try:
        # Prefer consolidated facts (already distilled by LLM)
        result = await db.db_query(
            "SELECT content FROM long_term_memory "
            "WHERE user_id = $1 AND source_type = 'consolidated' "
            "ORDER BY created_at DESC LIMIT 5",
            (user_id,),
        )
        # Fallback to raw user intents
        if not result:
            result = await db.db_query(
                "SELECT content FROM long_term_memory "
                "WHERE user_id = $1 AND source_type = 'user_intent' "
                "ORDER BY created_at DESC LIMIT 10",
                (user_id,),
            )
        if not result:
            return []

        # Extract key topics from recent memories
        topics: list[str] = []
        for row in result:
            content = row.get("content", "")
            if content and len(content) > 20:
                # Take first meaningful sentence
                first_sentence = content.split(".")[0].strip()
                if first_sentence and len(first_sentence) > 10:
                    topics.append(first_sentence[:100])

        return topics[:5]  # Limit to 5 topics
    except Exception as e:
        logger.error("Error getting user topics: %s", e, exc_info=True)
        return []


async def _search_for_topics(topics: list[str]) -> list[dict[str, str]]:
    """Search web for fresh content on user's topics using Tavily."""
    articles: list[dict[str, str]] = []

    try:
        from app.search_services import tavily_search_agent

        for topic in topics[:3]:  # Limit API calls
            try:
                response = await tavily_search_agent(
                    topic,
                    search_type="search",
                    max_results=2,
                )
                results = response.get("results", [])
                if results:
                    for r in results:
                        articles.append(
                            {
                                "title": r.get("title", ""),
                                "content": r.get("content", "")[:500],
                                "url": r.get("url", ""),
                            }
                        )
            except Exception as e:
                logger.debug("Tavily search failed for topic '%s': %s", topic[:30], e)
                continue

    except ImportError:
        logger.warning("Tavily search client not available for briefs")

    return articles[:5]  # Limit total articles


async def _generate_brief_summary(topics: list[str], articles: list[dict[str, str]]) -> dict[str, str]:
    """Use Gemini to produce per-topic summaries for digestible expandable blockquotes.

    Returns a dict mapping topic headline → 2-3 sentence summary string.
    Falls back to an empty dict on error.
    """
    if not topics and not articles:
        return {}

    try:
        from google.genai import types

        from app.providers.gemini import get_cached_genai_client
        from app.repos.keys import get_available_gemini_key

        key_data = await get_available_gemini_key(model_name="gemini-3.1-flash-lite")
        if not key_data:
            logger.warning("No Gemini API key available for brief generation.")
            return {}

        client = get_cached_genai_client(key_data["api_key"])

        articles_block = ""
        if articles:
            articles_block = "\n\nFresh articles:\n" + "\n".join(
                f"- [{a['title']}]({a['url']}): {a['content'][:300]}" for a in articles
            )

        prompt = (
            "You are an intelligence briefing assistant. "
            "Given the topics and articles below, produce a JSON object where each "
            "key is a concise topic headline (max 6 words, same language as topics) "
            "and the value is a 2-3 sentence summary for that topic. "
            "Include a source URL inline if available. "
            "Output ONLY valid JSON, no markdown fences.\n\n"
            "Topics:\n" + "\n".join(f"- {tp}" for tp in topics) + articles_block
        )

        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=1200),
        )
        raw = (response.text or "").strip()
        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)  # type: ignore[return-value]

    except Exception as e:
        logger.error("Brief generation failed: %s", e, exc_info=True)
        return {}


async def generate_and_send_brief(user_id: int, bot) -> bool:
    """Full pipeline: topics → search → per-topic LLM summaries → HTML digest.

    The digest uses Telegram's native <blockquote expandable> tags so each
    topic section is collapsed by default. A <tg-time> header shows the
    delivery time localized to the user's Telegram client timezone.
    """
    try:
        topics = await _get_user_topics(user_id)
        if not topics:
            logger.info("No LTM topics for user %s, skipping brief", user_id)
            return False

        articles = await _search_for_topics(topics)
        sections: dict[str, str] = await _generate_brief_summary(topics, articles)

        if not sections:
            logger.info("Empty brief generated for user %s, skipping", user_id)
            return False

        # ── Build HTML digest with expandable blockquotes ─────────────────
        import html as _html

        now_ts = int(time.time())
        time_tag = tg_time_tag(now_ts, fmt="f")  # "March 20, 2026 07:00"
        lines: list[str] = [
            f"<b>📬 Утренний брифинг</b> · {time_tag}\n",
        ]

        for headline, body in sections.items():
            safe_headline = _html.escape(headline)
            safe_body = _html.escape(body) if body else ""
            # Each topic as a collapsible blockquote: headline visible, body collapsed
            block = wrap_in_expandable_blockquote(safe_body, label=safe_headline)
            lines.append(block)
            lines.append("")  # visual spacing

        html_message = "\n".join(lines).strip()

        await bot.send_message(
            chat_id=user_id,
            text=html_message,
            parse_mode="HTML",
        )

        await mark_sent(user_id)
        logger.info("Brief sent to user %s (%d sections)", user_id, len(sections))
        return True

    except Exception as e:
        logger.error("Failed to send brief to user %s: %s", user_id, e, exc_info=True)
        return False


# ── Telegram command handlers ────────────────────────────────────────────


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /subscribe command."""
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    args = context.args or []
    sub_type = args[0] if args else "morning_brief"

    preferred_hour = 7
    if len(args) > 1:
        try:
            preferred_hour = parse_brief_schedule(args[1])
        except ValueError as e:
            await update.message.reply_text(str(e))
            return

    success = await upsert_subscription(
        user_id=user_id,
        sub_type=sub_type,
        is_active=True,
        preferred_hour=preferred_hour,
    )

    if success:
        await update.message.reply_text(
            t("brief.subscribed", type=sub_type, hour=str(preferred_hour)),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(t("brief.subscribe_error"))


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unsubscribe command."""
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    args = context.args or []
    sub_type = args[0] if args else "morning_brief"

    success = await deactivate_subscription(user_id=user_id, sub_type=sub_type)

    if success:
        await update.message.reply_text(
            t("brief.unsubscribed", type=sub_type),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(t("brief.unsubscribe_error"))


# ── Scheduler job ────────────────────────────────────────────────────────


async def check_and_send_briefs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly job: check for due subscriptions and send briefs.

    ⚡ Bolt: previously iterated sequentially — each LLM+Tavily call blocked the
    next user. Now all deliveries run concurrently, bounded by a semaphore of 10
    to avoid hammering provider APIs. Wall-clock time drops from O(n * t_brief)
    to roughly O(t_brief) regardless of subscriber count.
    """
    current_hour = datetime.now(tz=UTC).hour

    due_subs = await get_due_subscriptions(current_hour)
    if not due_subs:
        return

    logger.info("Found %d due subscriptions for hour %d UTC", len(due_subs), current_hour)

    # Concurrency cap: 10 simultaneous LLM+search chains prevent provider overload.
    sem = asyncio.Semaphore(10)

    async def _send_one(sub: dict) -> None:
        async with sem:
            try:
                await generate_and_send_brief(sub["user_id"], context.bot)
            except Exception as e:
                logger.error("Failed to process brief for user %s: %s", sub["user_id"], e)

    await asyncio.gather(*[_send_one(sub) for sub in due_subs])
