"""Tests for scheduled intelligence briefs.

Validates:
1. Subscription CRUD (create, read, deactivate).
2. Due subscriptions query.
3. Brief generation with mocked Tavily/Gemini.
4. Full pipeline integration.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _allow_private_data_lease_boundary():
    @asynccontextmanager
    async def allowed(*_args, **_kwargs):
        yield True

    with patch("app.repos.memory_consent.private_data_lease", allowed):
        yield


class TestParseBriefSchedule:
    """Test the schedule string parsing logic."""

    def test_parse_valid_formats(self):
        from app.handlers.scheduled_briefs import parse_brief_schedule

        assert parse_brief_schedule("8") == 8
        assert parse_brief_schedule("08:00") == 8
        assert parse_brief_schedule("23:59") == 23
        assert parse_brief_schedule(" 0:00 ") == 0
        assert parse_brief_schedule("12") == 12

    def test_parse_invalid_formats(self):
        from app.handlers.scheduled_briefs import parse_brief_schedule

        with pytest.raises(ValueError, match="Time string cannot be empty"):
            parse_brief_schedule("")

        with pytest.raises(ValueError, match="Time string cannot be empty"):
            parse_brief_schedule("   ")

        with pytest.raises(ValueError, match="Invalid time format: abc"):
            parse_brief_schedule("abc")

        with pytest.raises(ValueError, match="Hour must be between 0 and 23, got 24"):
            parse_brief_schedule("24:00")

        with pytest.raises(ValueError, match="Hour must be between 0 and 23, got -1"):
            parse_brief_schedule("-1")

        with pytest.raises(ValueError, match="Hour must be between 0 and 23, got 25"):
            parse_brief_schedule("25")


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
        with patch(
            "app.handlers.scheduled_briefs._query_ltm_topic_rows",
            new_callable=AsyncMock,
            return_value=[],
        ):
            from app.handlers.scheduled_briefs import generate_and_send_brief

            mock_bot = AsyncMock()
            result = await generate_and_send_brief(42, mock_bot)
            assert result is False
            mock_bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_and_send_brief_success(self):
        """Full pipeline with mocked LTM, Tavily, and Gemini."""
        mock_memory_rows = [
            {
                "content": "Discussing machine learning architectures and transformer models.",
                "memory_epoch": 9,
            },
            {
                "content": "Analyzing Python performance optimization techniques for production.",
                "memory_epoch": 9,
            },
        ]

        call_count = 0

        async def mock_topic_rows(user_id, source_type, limit):
            nonlocal call_count
            call_count += 1
            assert user_id == 42
            if source_type == "consolidated":
                return []  # No consolidated facts, triggers fallback
            if source_type == "user_intent":
                return mock_memory_rows
            return None

        with (
            patch(
                "app.handlers.scheduled_briefs._query_ltm_topic_rows",
                new_callable=AsyncMock,
                side_effect=mock_topic_rows,
            ),
            patch("app.handlers.scheduled_briefs._search_for_topics", new_callable=AsyncMock) as mock_search,
            patch("app.handlers.scheduled_briefs._generate_brief_summary", new_callable=AsyncMock) as mock_summary,
            patch(
                "app.handlers.scheduled_briefs._is_ltm_snapshot_current",
                new_callable=AsyncMock,
                return_value=True,
            ) as consent_check,
        ):
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
            mock_search.assert_awaited_once_with(mock_search.await_args.args[0], user_id=42, expected_epoch=9)
            mock_summary.assert_awaited_once_with(
                mock_summary.await_args.args[0],
                mock_summary.await_args.args[1],
                user_id=42,
                expected_epoch=9,
            )
            consent_check.assert_awaited_once_with(42, 9)

            # Verify the message contains the brief
            sent_text = mock_bot.send_message.call_args[1]["text"]
            assert "бриф" in sent_text.lower() or "ML" in sent_text


class TestTopicSearch:
    """Test the scheduled brief adapter around Tavily search results."""

    @pytest.mark.asyncio
    async def test_search_for_topics_parses_tavily_search_envelope(self):
        tavily_response = {
            "type": "search",
            "results": [
                {
                    "title": "Fresh result",
                    "content": "Current information about the topic.",
                    "url": "https://example.com/article",
                    "score": 0.95,
                }
            ],
        }

        with patch(
            "app.search_services.tavily_search_agent",
            new_callable=AsyncMock,
            return_value=tavily_response,
        ) as mock_search:
            from app.handlers.scheduled_briefs import _search_for_topics

            articles = await _search_for_topics(["machine learning"])

        assert articles == [
            {
                "title": "Fresh result",
                "content": "Current information about the topic.",
                "url": "https://example.com/article",
            }
        ]
        mock_search.assert_awaited_once_with(
            "machine learning",
            search_type="search",
            max_results=2,
        )

    @pytest.mark.asyncio
    async def test_search_rechecks_epoch_immediately_before_each_tavily_call(self):
        tavily_response = {"type": "search", "results": []}

        with (
            patch(
                "app.handlers.scheduled_briefs._is_ltm_snapshot_current",
                new_callable=AsyncMock,
                side_effect=[True, False],
            ) as consent_check,
            patch(
                "app.search_services.tavily_search_agent",
                new_callable=AsyncMock,
                return_value=tavily_response,
            ) as mock_search,
        ):
            from app.handlers.scheduled_briefs import _search_for_topics

            await _search_for_topics(
                ["first private topic", "second private topic"],
                user_id=42,
                expected_epoch=9,
            )

        assert consent_check.await_count == 2
        assert all(call.args == (42, 9) for call in consent_check.await_args_list)
        mock_search.assert_awaited_once_with(
            "first private topic",
            search_type="search",
            max_results=2,
        )

    @pytest.mark.asyncio
    async def test_summary_rechecks_epoch_immediately_before_gemini_call(self):
        client = MagicMock()
        client.aio.models.generate_content = AsyncMock()

        with (
            patch(
                "app.handlers.scheduled_briefs._is_ltm_snapshot_current",
                new_callable=AsyncMock,
                return_value=False,
            ) as consent_check,
            patch(
                "app.repos.keys.get_available_gemini_key",
                new_callable=AsyncMock,
                return_value={"api_key": "key"},
            ),
            patch("app.providers.gemini.get_cached_genai_client", return_value=client),
        ):
            from app.handlers.scheduled_briefs import _generate_brief_summary

            result = await _generate_brief_summary(
                ["private topic"],
                [],
                user_id=42,
                expected_epoch=9,
            )

        assert result == {}
        consent_check.assert_awaited_once_with(42, 9)
        client.aio.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_brief_rechecks_epoch_before_delivery():
    mock_bot = AsyncMock()

    with (
        patch(
            "app.handlers.scheduled_briefs._get_user_topics_snapshot",
            new_callable=AsyncMock,
            return_value=(["private topic"], 9),
        ),
        patch(
            "app.handlers.scheduled_briefs._search_for_topics",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.handlers.scheduled_briefs._generate_brief_summary",
            new_callable=AsyncMock,
            return_value={"Topic": "Summary"},
        ),
        patch(
            "app.handlers.scheduled_briefs._is_ltm_snapshot_current",
            new_callable=AsyncMock,
            return_value=False,
        ) as consent_check,
    ):
        from app.handlers.scheduled_briefs import generate_and_send_brief

        result = await generate_and_send_brief(42, mock_bot)

    assert result is False
    consent_check.assert_awaited_once_with(42, 9)
    mock_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ltm_topics_require_enabled_consent_and_live_rows():
    from app.handlers.scheduled_briefs import _query_ltm_topic_rows

    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    with (
        patch("app.handlers.scheduled_briefs.db.db_manager.pool", pool),
        patch("app.handlers.scheduled_briefs.db.set_user_context", new_callable=AsyncMock) as set_context,
        patch("app.handlers.scheduled_briefs.db.clear_user_context", new_callable=AsyncMock) as clear_context,
        patch("app.handlers.scheduled_briefs.db.db_query", new_callable=AsyncMock, return_value=[]) as query_db,
    ):
        assert await _query_ltm_topic_rows(42, "consolidated", 5) == []

    query = query_db.await_args.args[0]
    assert "JOIN chats" in query
    assert "ltm_enabled" in query
    assert "memory_epoch" in query
    assert "expires_at" in query
    assert query_db.await_args.kwargs["conn"] is conn
    set_context.assert_awaited_once_with(42, False, conn=conn)
    clear_context.assert_awaited_once_with(conn=conn)
