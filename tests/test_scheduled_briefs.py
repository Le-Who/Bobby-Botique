"""Tests for scheduled intelligence briefs.

Validates:
1. Subscription CRUD (create, read, deactivate).
2. Due subscriptions query.
3. Brief generation with mocked Tavily/Gemini.
4. Full pipeline integration.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSubscriptionCRUD:
    """Test subscription database operations."""

    @pytest.mark.asyncio
    async def test_upsert_subscription(self):
        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=None)

            from app.handlers.scheduled_briefs import upsert_subscription

            result = await upsert_subscription(user_id=42, sub_type="morning_brief", preferred_hour=8)
            assert result is True
            mock_db.db_query.assert_awaited_once()

            # Verify the query contains UPSERT logic
            query = mock_db.db_query.call_args[0][0]
            assert "INSERT INTO brief_subscriptions" in query
            assert "ON CONFLICT" in query

    @pytest.mark.asyncio
    async def test_get_subscription_found(self):
        mock_row = {
            "id": 1,
            "is_active": True,
            "timezone": "UTC",
            "preferred_hour": 7,
            "last_sent_at": None,
        }

        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=[mock_row])

            from app.handlers.scheduled_briefs import get_subscription

            result = await get_subscription(42)
            assert result is not None
            assert result["is_active"] is True
            assert result["preferred_hour"] == 7

    @pytest.mark.asyncio
    async def test_get_subscription_not_found(self):
        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=[])

            from app.handlers.scheduled_briefs import get_subscription

            result = await get_subscription(42)
            assert result is None

    @pytest.mark.asyncio
    async def test_deactivate_subscription(self):
        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=None)

            from app.handlers.scheduled_briefs import deactivate_subscription

            result = await deactivate_subscription(42)
            assert result is True
            query = mock_db.db_query.call_args[0][0]
            assert "is_active = FALSE" in query


class TestDueSubscriptions:
    """Test the scheduler query."""

    @pytest.mark.asyncio
    async def test_get_due_subscriptions(self):
        mock_rows = [
            {
                "user_id": 42,
                "subscription_type": "morning_brief",
                "timezone": "UTC",
                "preferred_hour": 7,
                "last_sent_at": None,
            },
            {
                "user_id": 99,
                "subscription_type": "morning_brief",
                "timezone": "UTC",
                "preferred_hour": 7,
                "last_sent_at": None,
            },
        ]

        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=mock_rows)

            from app.handlers.scheduled_briefs import get_due_subscriptions

            result = await get_due_subscriptions(7)
            assert len(result) == 2
            assert result[0]["user_id"] == 42

    @pytest.mark.asyncio
    async def test_no_due_subscriptions(self):
        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=[])

            from app.handlers.scheduled_briefs import get_due_subscriptions

            result = await get_due_subscriptions(3)
            assert result == []


class TestBriefGeneration:
    """Test brief generation pipeline with mocked external services."""

    @pytest.mark.asyncio
    async def test_generate_and_send_brief_no_topics(self):
        """If user has no LTM, brief should be skipped."""
        with patch("app.handlers.scheduled_briefs.db") as mock_db:
            mock_db.db_query = AsyncMock(return_value=[])

            from app.handlers.scheduled_briefs import generate_and_send_brief

            mock_bot = AsyncMock()
            result = await generate_and_send_brief(42, mock_bot)
            assert result is False
            mock_bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_and_send_brief_success(self):
        """Full pipeline with mocked LTM, Tavily, and Gemini."""
        mock_memory_rows = [
            {"content": "Discussing machine learning architectures and transformer models."},
            {"content": "Analyzing Python performance optimization techniques for production."},
        ]

        call_count = 0

        async def mock_db_query(query, params=None):
            nonlocal call_count
            call_count += 1
            if "long_term_memory" in query and "consolidated" in query:
                return []  # No consolidated facts, triggers fallback
            if "long_term_memory" in query and "user_intent" in query:
                return mock_memory_rows
            return None

        with (
            patch("app.handlers.scheduled_briefs.db") as mock_db,
            patch("app.handlers.scheduled_briefs._search_for_topics", new_callable=AsyncMock) as mock_search,
            patch("app.handlers.scheduled_briefs._generate_brief_summary", new_callable=AsyncMock) as mock_summary,
        ):
            mock_db.db_query = AsyncMock(side_effect=mock_db_query)
            mock_search.return_value = [{"title": "Test", "content": "Test content", "url": "https://test.com"}]
            mock_summary.return_value = {
                "🤖 ML архитектуры": "Point 1: Transformers.",
                "🐍 Python": "Point 2: Performance.",
            }

            from app.handlers.scheduled_briefs import generate_and_send_brief

            mock_bot = AsyncMock()
            result = await generate_and_send_brief(42, mock_bot)
            assert result is True
            mock_bot.send_message.assert_awaited_once()

            # Verify the message contains the brief
            sent_text = mock_bot.send_message.call_args[1]["text"]
            assert "бриф" in sent_text.lower() or "ML" in sent_text
