"""
Document repository — database operations for user documents.

Extracted from ``DocumentProcessor`` to separate I/O-bound DB
operations from CPU-bound file parsing.
"""

import logging
from typing import Any
from uuid import uuid4

import asyncpg

from app import database
from app.config import settings
from app.repos.users import is_admin

logger = logging.getLogger(__name__)


# ============================================================================
# DUPLICATE CHECK
# ============================================================================


async def _find_duplicate_document(user_id: int, file_hash: str, *, conn=None) -> dict[str, Any] | None:
    result = await database.db_query(
        """
        SELECT id, filename, created_at FROM user_documents
        WHERE user_id = $1 AND split_part(file_hash, ':', 1) = $2
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, file_hash),
        conn=conn,
    )
    return dict(result[0]) if result else None


async def check_duplicate_file(
    user_id: int, file_hash: str, filename: str, *, raise_on_error: bool = False
) -> dict[str, Any] | None:
    """Find duplicates; strict upload callers must distinguish an outage from absence."""
    try:
        return await _find_duplicate_document(user_id, file_hash)
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        if raise_on_error:
            raise
        logger.error("Error checking duplicate file: %s", e, exc_info=True)
        return None


# ============================================================================
# CRUD
# ============================================================================


async def save_document_content(
    user_id: int, filename: str, content: str, pages: int, file_hash: str, *, skip_duplicate_check: bool = False
) -> dict[str, Any] | None:
    """Commit a parsed document and its retention changes as one operation.

    Parsing happens before this short transaction. The user advisory lock also
    coordinates queued uploads and the existing account-erasure boundary.
    Errors, including a failed commit, belong to the upload caller.

    Return existing metadata instead of mutating on an ordinary duplicate.
    ``file_hash`` is the canonical SHA-256 of the input. Intentional copies store
    ``<sha256>:copy:<uuidhex>`` in the existing TEXT column to respect its
    UNIQUE(user_id, file_hash) constraint. Both lookups use the canonical prefix
    so a retained copy remains detectable after the original row is removed.
    """
    keep_count = settings.MAX_DOCUMENTS_PER_USER
    if keep_count < 1:
        raise ValueError("Document capacity must be positive")
    if not database.db_manager.is_connected:
        await database.reconnect_database()

    async with database.db_manager.pool.acquire() as conn, conn.transaction():
        await database.set_user_context(user_id, is_admin(user_id), conn=conn)
        try:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            if not skip_duplicate_check:
                duplicate = await _find_duplicate_document(user_id, file_hash, conn=conn)
                if duplicate:
                    return duplicate
            stored_hash = f"{file_hash}:copy:{uuid4().hex}" if skip_duplicate_check else file_hash
            await database.db_query(
                "INSERT INTO user_documents (user_id, filename, content, pages, file_size, file_hash, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, clock_timestamp())",
                (user_id, filename, content, pages, len(content), stored_hash),
                conn=conn,
            )
            await database.db_query(
                """
                DELETE FROM user_documents
                WHERE user_id = $1 AND id IN (
                    SELECT id FROM user_documents
                    WHERE user_id = $1
                    ORDER BY created_at DESC, id DESC
                    OFFSET $2
                )
                """,
                (user_id, keep_count),
                conn=conn,
            )
        finally:
            await database.clear_user_context(conn=conn)
    logger.info("Saved document %s for user %s", filename, user_id)
    return None


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
                "created_at": (row["created_at"].isoformat() if row["created_at"] else None),
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
        async with database.db_manager.pool.acquire() as conn, conn.transaction():
            await database.set_user_context(user_id, is_admin(user_id), conn=conn)

            try:
                result = await database.db_query(
                    "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC",
                    (user_id,),
                    conn=conn,
                )

                return [
                    {
                        "id": row["id"],
                        "filename": row["filename"],
                        "pages": row["pages"],
                        "created_at": (row["created_at"].isoformat() if row["created_at"] else None),
                        "file_size": row["file_size"],
                        "file_hash": row["file_hash"],
                    }
                    for row in result
                ]
            finally:
                await database.clear_user_context(conn=conn)

    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logger.error("Error getting user documents: %s", e, exc_info=True)
        return []


async def get_document_content(document_id: int, user_id: int) -> str | None:
    """Retrieve the text content of a document (RLS-aware)."""
    try:
        async with database.db_manager.pool.acquire() as conn, conn.transaction():
            await database.set_user_context(user_id, is_admin(user_id), conn=conn)

            try:
                result = await database.db_query(
                    "SELECT content FROM user_documents WHERE id = $1 AND user_id = $2",
                    (document_id, user_id),
                    conn=conn,
                )

                if result:
                    return result[0]["content"]
                return None
            finally:
                await database.clear_user_context(conn=conn)

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
        raise


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
                "total_size_mb": (stats["total_size"] / (1024 * 1024) if stats["total_size"] else 0),
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
                "total_size_mb": (stats["total_size"] / (1024 * 1024) if stats["total_size"] else 0),
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
