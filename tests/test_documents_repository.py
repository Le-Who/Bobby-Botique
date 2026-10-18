"""Tests for app.documents.repository — database CRUD operations."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.documents.repository import (
    check_duplicate_file,
    delete_all_user_documents,
    delete_document,
    get_document_stats,
    get_user_document_stats,
    save_document_content,
)

# ── check_duplicate_file ─────────────────────────────────────────────────────


class TestCheckDuplicateFile:
    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_duplicate_info(self, mock_db):
        mock_db.db_query = AsyncMock(
            return_value=[
                {
                    "id": 42,
                    "filename": "test.pdf",
                    "created_at": "2026-01-01",
                }
            ]
        )
        result = await check_duplicate_file(1, "abc123", "test.pdf")
        assert result is not None
        assert result["id"] == 42
        assert result["filename"] == "test.pdf"

    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_none_when_no_duplicate(self, mock_db):
        mock_db.db_query = AsyncMock(return_value=[])
        result = await check_duplicate_file(1, "abc123", "test.pdf")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_none_on_db_error(self, mock_db):
        import asyncpg

        mock_db.db_query = AsyncMock(side_effect=asyncpg.PostgresError("connection error"))
        result = await check_duplicate_file(1, "abc123", "test.pdf")
        assert result is None


# ── save_document_content ────────────────────────────────────────────────────


class TestSaveDocumentContent:
    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_inserts_with_correct_params(self, mock_db):
        mock_db.db_query = AsyncMock(return_value=None)
        mock_db.set_user_context = AsyncMock()
        mock_db.clear_user_context = AsyncMock()
        conn = MagicMock()
        conn.execute = AsyncMock()
        mock_db.db_manager.pool.acquire.return_value.__aenter__.return_value = conn
        await save_document_content(1, "test.pdf", "content here", 5, "hash123")
        args = next(
            call.args for call in mock_db.db_query.await_args_list if "INSERT INTO user_documents" in call.args[0]
        )
        assert "INSERT INTO user_documents" in args[0]
        assert args[1] == (
            1,
            "test.pdf",
            "content here",
            5,
            len("content here"),
            "hash123",
        )


# ── delete_document ──────────────────────────────────────────────────────────


class TestDeleteDocument:
    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_true_on_success(self, mock_db):
        mock_db.db_query = AsyncMock(return_value=None)
        result = await delete_document(42, 1)
        assert result is True

    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_false_on_error(self, mock_db):
        import asyncpg

        mock_db.db_query = AsyncMock(side_effect=asyncpg.PostgresError("oops"))
        result = await delete_document(42, 1)
        assert result is False


# ── delete_all_user_documents ────────────────────────────────────────────────


class TestDeleteAllUserDocuments:
    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_count(self, mock_db):
        mock_db.db_query = AsyncMock(return_value=[{"id": 1}, {"id": 2}, {"id": 3}])
        count = await delete_all_user_documents(1)
        assert count == 3

    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_zero_when_none(self, mock_db):
        mock_db.db_query = AsyncMock(return_value=None)
        count = await delete_all_user_documents(1)
        assert count == 0


# ── get_document_stats ───────────────────────────────────────────────────────


class TestGetDocumentStats:
    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_stats(self, mock_db):
        mock_db.db_query = AsyncMock(
            return_value=[
                {
                    "doc_count": 10,
                    "total_size": 50000,
                    "avg_size": 5000,
                }
            ]
        )
        stats = await get_document_stats()
        assert stats["total_documents"] == 10
        assert stats["total_size_chars"] == 50000
        assert "total_size_mb" in stats

    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_zeros_on_error(self, mock_db):
        import asyncpg

        mock_db.db_query = AsyncMock(side_effect=asyncpg.PostgresError("fail"))
        stats = await get_document_stats()
        assert stats["total_documents"] == 0
        assert stats["total_size_mb"] == 0


# ── get_user_document_stats ──────────────────────────────────────────────────


class TestGetUserDocumentStats:
    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_returns_user_stats(self, mock_db):
        mock_db.db_query = AsyncMock(
            side_effect=[
                [{"doc_count": 3}],
                [{"total_size": 15000, "avg_size": 5000}],
            ]
        )
        stats = await get_user_document_stats(1)
        assert stats["document_count"] == 3
        assert stats["can_upload"] is True

    @pytest.mark.asyncio
    @patch("app.documents.repository.database")
    async def test_limit_reached_at_five(self, mock_db):
        mock_db.db_query = AsyncMock(
            side_effect=[
                [{"doc_count": 5}],
                [{"total_size": 25000, "avg_size": 5000}],
            ]
        )
        stats = await get_user_document_stats(1)
        assert stats["limit_reached"] is True
        assert stats["can_upload"] is False
