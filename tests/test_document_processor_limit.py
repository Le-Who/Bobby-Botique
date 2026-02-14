import pytest
import asyncio
import io
import sys
from docx import Document
from unittest.mock import MagicMock, AsyncMock, patch

# Define fixtures to handle mocking
@pytest.fixture(autouse=True)
def mock_dependencies():
    """Mock dependencies for DocumentProcessor."""
    mock_db = MagicMock()
    mock_metrics = MagicMock()
    mock_metrics.metrics_collector = AsyncMock()
    mock_metrics.metrics_collector.record_error = AsyncMock()

    with patch.dict(sys.modules, {
        "app.database": mock_db,
        "app.metrics": mock_metrics
    }):
        if "app.document_processor" in sys.modules:
            import importlib
            import app.document_processor
            importlib.reload(app.document_processor)
        else:
            import app.document_processor
        yield

@pytest.fixture
def document_processor():
    from app.document_processor import DocumentProcessor
    processor = DocumentProcessor()
    processor._check_document_limit = AsyncMock(return_value=True)
    processor._cleanup_oldest_documents = AsyncMock()
    processor._check_duplicate_file = AsyncMock(return_value=None)
    processor._save_document_content = AsyncMock()
    return processor

def create_large_docx_bytes(target_length=110000):
    doc = Document()
    # Create chunks of 1000 chars
    chunk = "A" * 1000
    num_chunks = (target_length // 1000) + 1

    for _ in range(num_chunks):
        doc.add_paragraph(chunk)

    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()

@pytest.mark.asyncio
async def test_process_word_limit(document_processor):
    # Create a docx larger than 100,000 chars
    docx_bytes = create_large_docx_bytes(target_length=110000)
    filename = "large.docx"
    user_id = 123
    file_hash = "fakehash"

    result = await document_processor._process_word(docx_bytes, filename, user_id, file_hash)

    assert result["success"] is True
    content = result["content"]

    # Check if content length is limited to roughly 100,000
    # It might be slightly more because we check after adding a paragraph
    # But it should be significantly less than 110,000 if we stop early
    # Wait, existing implementation returns FULL content.

    # We expect this assertion to FAIL before the fix
    assert len(content) < 105000, f"Content length {len(content)} is too large"

    # Verify truncation message is appended (if we decide to add one)
    # assert "truncated" in content
