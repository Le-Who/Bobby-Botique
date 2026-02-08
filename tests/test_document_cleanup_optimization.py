import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import sys

# Mock all dependencies BEFORE importing app.document_processor
mock_db = MagicMock()
mock_db.db_query = AsyncMock()

mock_metrics = MagicMock()
mock_metrics.metrics_collector = AsyncMock()

mock_config = MagicMock()
mock_config.settings = MagicMock()
mock_config.settings.MAX_DOCUMENTS_PER_USER = 5

mock_pypdf = MagicMock()
mock_docx = MagicMock()
mock_httpx = MagicMock()

modules_to_patch = {
    "app.database": mock_db,
    "app.metrics": mock_metrics,
    "app.config": mock_config,
    "pypdf": mock_pypdf,
    "docx": mock_docx,
    "httpx": mock_httpx,
    "app.utils.network": MagicMock(),
}

with patch.dict(sys.modules, modules_to_patch):
    from app.document_processor import DocumentProcessor

@pytest.fixture
def document_processor():
    processor = DocumentProcessor()
    # Reset mock
    mock_db.db_query.reset_mock()
    return processor

def test_cleanup_oldest_documents_single_query_behavior(document_processor):
    """
    Verifies that the optimized implementation uses a single query.
    """
    async def run_test():
        user_id = 123
        keep_count = 4

        # Setup mock return for the query (DELETE ... RETURNING id)
        # We expect it to return the deleted ID(s)
        mock_db.db_query.return_value = [{'id': 105}]

        deleted_count = await document_processor._cleanup_oldest_documents(user_id, keep_count)

        assert deleted_count == 1

        # Verify only ONE call was made
        assert mock_db.db_query.call_count == 1

        calls = mock_db.db_query.call_args_list

        # Single call: DELETE ... WHERE id IN (SELECT ...)
        sql = calls[0][0][0]
        params = calls[0][0][1]

        # Normalize whitespace for comparison
        sql_norm = ' '.join(sql.split())

        assert "DELETE FROM user_documents" in sql_norm
        assert "WHERE id IN" in sql_norm
        assert "SELECT id FROM user_documents" in sql_norm
        assert "WHERE user_id = $1" in sql_norm
        assert "ORDER BY created_at ASC" in sql_norm
        assert "OFFSET $2" in sql_norm
        assert "RETURNING id" in sql_norm # Ensure we return IDs for count

        assert params == (user_id, keep_count)

    asyncio.run(run_test())
