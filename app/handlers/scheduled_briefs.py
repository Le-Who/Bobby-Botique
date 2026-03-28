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

import logging
from datetime import UTC, datetime
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from app import database as db
from app.i18n import t
from app.repos.users import is_admin

logger = logging.getLogger(__name__)


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
            "ORDER BY updated_at DESC LIMIT 5",
            (user_id,),
        )
        # Fallback to raw user intents
        if not result:
            result = await db.db_query(
                "SELECT content FROM long_term_memory "
                "WHERE user_id = $1 AND source_type = 'user_intent' "
                "ORDER BY updated_at DESC LIMIT 10",
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
        from app.research.search_client import search_tavily

        for topic in topics[:3]:  # Limit API calls
            try:
                results = await search_tavily(topic, max_results=2)
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


async def _generate_brief_summary(topics: list[str], articles: list[dict[str, str]]) -> str:
    """Use Gemini to create a concise brief from topics and articles."""
    if not topics and not articles:
        return ""

    try:
        import google.generativeai as genai

        model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")

        articles_text = ""
        if articles:
            articles_text = "\n\nFresh articles found:\n" + "\n".join(
                f"- {a['title']}: {a['content'][:200]}..." for a in articles
            )

        prompt = f"""Create a concise morning intelligence brief (3-5 bullet points) based on:

User's recent topics of interest:
{chr(10).join(f"- {t}" for t in topics)}

{articles_text}

Format: Use bullet points (•). Keep each point to 1-2 sentences.
Add relevant article links where available.
Write in the same language as the user's topics."""

        response = await model.generate_content_async(prompt)
        return response.text if response.text else ""

    except Exception as e:
        logger.error("Brief generation failed: %s", e, exc_info=True)
        return ""


async def generate_and_send_brief(user_id: int, bot) -> bool:
    """Full pipeline: topics → search → summarize → send."""
    try:
        topics = await _get_user_topics(user_id)
        if not topics:
            logger.info("No LTM topics for user %s, skipping brief", user_id)
            return False

        articles = await _search_for_topics(topics)
        summary = await _generate_brief_summary(topics, articles)

        if not summary:
            logger.info("Empty brief generated for user %s, skipping", user_id)
            return False

        message = t("brief.morning_title", summary=summary)
        await bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")

        await mark_sent(user_id)
        logger.info("Brief sent to user %s", user_id)
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
    preferred_hour = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7

    success = await upsert_subscription(
        user_id=user_id,
        sub_type=sub_type,
        is_active=True,
        preferred_hour=preferred_hour,
    )

    if success:
        await update.message.reply_text(
            t("brief.subscribed", type=sub_type, hour=str(preferred_hour)),
            parse_mode="Markdown",
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
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(t("brief.unsubscribe_error"))


# ── Scheduler job ────────────────────────────────────────────────────────


async def check_and_send_briefs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly job: check for due subscriptions and send briefs."""
    current_hour = datetime.now(tz=UTC).hour

    due_subs = await get_due_subscriptions(current_hour)
    if not due_subs:
        return

    logger.info("Found %d due subscriptions for hour %d UTC", len(due_subs), current_hour)

    for sub in due_subs:
        try:
            await generate_and_send_brief(sub["user_id"], context.bot)
        except Exception as e:
            logger.error("Failed to process brief for user %s: %s", sub["user_id"], e)
