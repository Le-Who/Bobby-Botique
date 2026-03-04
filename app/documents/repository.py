"""
Document repository — database operations for user documents.

Extracted from ``DocumentProcessor`` to separate I/O-bound DB
operations from CPU-bound file parsing.
"""

import logging
from typing import Any

import asyncpg

from app import database
from app.config import settings
from app.repos.users import is_admin

logger = logging.getLogger(__name__)


# ============================================================================
# DUPLICATE & LIMIT CHECKS
# ============================================================================


async def check_duplicate_file(user_id: int, file_hash: str, filename: str) -> dict[str, Any] | None:
    """Check if a file with the same hash already exists for the user."""
    try:
        result = await database.db_query(
            "SELECT id, filename, created_at FROM user_documents WHERE user_id = $1 AND file_hash = $2",
            (user_id, file_hash),
        )
        if result:
            return {
                "id": result[0]["id"],
                "filename": result[0]["filename"],
                "created_at": result[0]["created_at"],
            }
        return None
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error checking duplicate file: %s", e, exc_info=True)
        return None


async def check_document_limit(user_id: int) -> bool:
    """Return True if the user is below the document limit."""
    try:
        result = await database.db_query(
            "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
            (user_id,),
        )
        doc_count = result[0]["doc_count"] if result else 0
        return doc_count < settings.MAX_DOCUMENTS_PER_USER
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error checking document limit: %s", e, exc_info=True)
        return True  # Allow upload on error


async def cleanup_oldest_documents(user_id: int, keep_count: int = 4) -> int:
    """Delete oldest documents for a user, keeping *keep_count* newest."""
    try:
        result = await database.db_query(
            """
            DELETE FROM user_documents
            WHERE id IN (
                SELECT id FROM user_documents
                WHERE user_id = $1
                ORDER BY created_at DESC
                OFFSET $2
            )
            RETURNING id
        """,
            (user_id, keep_count),
        )

        if not result:
            return 0

        deleted_count = len(result)
        logger.info("Cleaned up %d oldest documents for user %s", deleted_count, user_id)
        return deleted_count

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error cleaning up oldest documents: %s", e, exc_info=True)
        return 0


# ============================================================================
# CRUD
# ============================================================================


async def save_document_content(user_id: int, filename: str, content: str, pages: int, file_hash: str) -> None:
    """Persist parsed document content to the database."""
    try:
        await database.db_query(
            "INSERT INTO user_documents (user_id, filename, content, pages, file_size, file_hash) VALUES ($1, $2, $3, $4, $5, $6)",
            (user_id, filename, content, pages, len(content), file_hash),
        )
        logger.info("Saved document %s for user %s", filename, user_id)
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error saving document to database: %s", e, exc_info=True)


async def get_document_by_id(document_id: int, user_id: int) -> dict[str, Any] | None:
    """Retrieve document metadata by ID."""
    try:
        result = await database.db_query(
            "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE id = $1 AND user_id = $2",
            (document_id, user_id),
        )

        if result:
            row = result[0]
            return {
                "id": row["id"],
                "filename": row["filename"],
                "pages": row["pages"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "file_size": row["file_size"],
                "file_hash": row["file_hash"],
            }
        return None

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error getting document by ID: %s", e, exc_info=True)
        return None


async def get_user_documents(user_id: int) -> list[dict[str, Any]]:
    """List all documents for a user (RLS-aware)."""
    try:
        await database.set_user_context(user_id, is_admin(user_id))

        try:
            result = await database.db_query(
                "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC",
                (user_id,),
            )

            return [
                {
                    "id": row["id"],
                    "filename": row["filename"],
                    "pages": row["pages"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "file_size": row["file_size"],
                    "file_hash": row["file_hash"],
                }
                for row in result
            ]
        finally:
            await database.clear_user_context()

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error getting user documents: %s", e, exc_info=True)
        return []


async def get_document_content(document_id: int, user_id: int) -> str | None:
    """Retrieve the text content of a document (RLS-aware)."""
    try:
        await database.set_user_context(user_id, is_admin(user_id))

        try:
            result = await database.db_query(
                "SELECT content FROM user_documents WHERE id = $1 AND user_id = $2",
                (document_id, user_id),
            )

            if result:
                return result[0]["content"]
            return None
        finally:
            await database.clear_user_context()

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error getting document content: %s", e, exc_info=True)
        return None


async def delete_document(document_id: int, user_id: int) -> bool:
    """Delete a single document."""
    try:
        await database.db_query(
            "DELETE FROM user_documents WHERE id = $1 AND user_id = $2",
            (document_id, user_id),
        )
        return True

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error deleting document: %s", e, exc_info=True)
        return False


async def delete_all_user_documents(user_id: int) -> int:
    """Delete all documents for a user. Returns count deleted."""
    try:
        result = await database.db_query(
            "DELETE FROM user_documents WHERE user_id = $1 RETURNING id",
            (user_id,),
        )
        return len(result) if result else 0

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error deleting all documents: %s", e, exc_info=True)
        return 0


async def cleanup_old_documents(days_old: int = 3) -> int:
    """Remove documents older than *days_old* days."""
    try:
        result = await database.db_query(
            """
            DELETE FROM user_documents
            WHERE created_at < (CURRENT_TIMESTAMP - ($1 * INTERVAL '1 day'))
            RETURNING id
        """,
            (days_old,),
        )

        deleted_count = len(result) if result else 0
        logger.info("Cleaned up %d old documents (older than %d days)", deleted_count, days_old)
        return deleted_count

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error cleaning up old documents: %s", e, exc_info=True)
        return 0


# ============================================================================
# STATS
# ============================================================================


async def get_document_stats() -> dict[str, Any]:
    """Aggregate stats across all documents."""
    try:
        size_result = await database.db_query("""
            SELECT
                COUNT(*) as doc_count,
                COALESCE(SUM(file_size), 0) as total_size,
                COALESCE(AVG(file_size), 0) as avg_size
            FROM user_documents
        """)

        if size_result:
            stats = size_result[0]
            return {
                "total_documents": stats["doc_count"],
                "total_size_chars": stats["total_size"],
                "average_size_chars": stats["avg_size"],
                "total_size_mb": stats["total_size"] / (1024 * 1024) if stats["total_size"] else 0,
            }

        return {
            "total_documents": 0,
            "total_size_chars": 0,
            "average_size_chars": 0,
            "total_size_mb": 0,
        }

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error getting document stats: %s", e, exc_info=True)
        return {
            "total_documents": 0,
            "total_size_chars": 0,
            "average_size_chars": 0,
            "total_size_mb": 0,
        }


async def get_user_document_stats(user_id: int) -> dict[str, Any]:
    """Per-user document statistics."""
    try:
        count_result = await database.db_query(
            "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
            (user_id,),
        )
        doc_count = count_result[0]["doc_count"] if count_result else 0

        size_result = await database.db_query(
            """
            SELECT
                COALESCE(SUM(file_size), 0) as total_size,
                COALESCE(AVG(file_size), 0) as avg_size
            FROM user_documents
            WHERE user_id = $1
        """,
            (user_id,),
        )

        if size_result:
            stats = size_result[0]
            return {
                "document_count": doc_count,
                "total_size_chars": stats["total_size"],
                "average_size_chars": stats["avg_size"],
                "total_size_mb": stats["total_size"] / (1024 * 1024) if stats["total_size"] else 0,
                "limit_reached": doc_count >= settings.MAX_DOCUMENTS_PER_USER,
                "can_upload": doc_count < settings.MAX_DOCUMENTS_PER_USER,
            }

        return {
            "document_count": 0,
            "total_size_chars": 0,
            "average_size_chars": 0,
            "total_size_mb": 0,
            "limit_reached": False,
            "can_upload": True,
        }

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error getting user document stats: %s", e, exc_info=True)
        return {
            "document_count": 0,
            "total_size_chars": 0,
            "average_size_chars": 0,
            "total_size_mb": 0,
            "limit_reached": False,
            "can_upload": True,
        }
