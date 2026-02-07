import pytest
import asyncio
import io
import sys
import pypdf
from docx import Document
from unittest.mock import MagicMock, AsyncMock, patch

# Define fixtures to handle mocking
@pytest.fixture(autouse=True)
def mock_dependencies():
    """Mock dependencies for DocumentProcessor without polluting global state permanently."""
    # Create mocks
    mock_db = MagicMock()
    mock_metrics = MagicMock()
    mock_metrics.metrics_collector = AsyncMock()
    mock_metrics.metrics_collector.record_error = AsyncMock()

    # Patch sys.modules
    with patch.dict(sys.modules, {
        "app.database": mock_db,
        "app.metrics": mock_metrics
    }):
        # Now import the module under test inside the patch context
        # If it was already imported, we need to reload it to pick up the mocks?
        # Or we can just import it here.
        # Since this test file is running in a process where app.document_processor
        # might not be imported yet, or if it is, it might have imported real modules.

        # To be safe, we should ensure app.document_processor uses our mocks.
        # But patching sys.modules only affects future imports.

        # If app.document_processor is already imported, we might need to reload it.
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
    # Mock database interactions
    processor._check_document_limit = AsyncMock(return_value=True)
    processor._cleanup_oldest_documents = AsyncMock()
    processor._check_duplicate_file = AsyncMock(return_value=None)
    processor._save_document_content = AsyncMock()
    return processor

def create_pdf_bytes():
    # Create a minimal PDF
    pdf_writer = pypdf.PdfWriter()
    page = pypdf.PageObject.create_blank_page(width=100, height=100)
    pdf_writer.add_page(page)

    output = io.BytesIO()
    pdf_writer.write(output)
    return output.getvalue()

def create_docx_bytes():
    # Create a minimal DOCX
    doc = Document()
    doc.add_paragraph("Hello World")

    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()

@pytest.mark.asyncio
async def test_process_pdf_with_streams(document_processor):
    pdf_bytes = create_pdf_bytes()
    filename = "test.pdf"
    user_id = 123
    file_hash = "fakehash"

    # Spy on _process_pdf_sync to ensure it receives BytesIO
    with patch.object(document_processor, "_process_pdf_sync", wraps=document_processor._process_pdf_sync) as mock_sync:
        result = await document_processor._process_pdf(pdf_bytes, filename, user_id, file_hash)

        assert result["success"] is True
        assert result["pages"] == 1

        # Verify _process_pdf_sync was called with BytesIO
        args, _ = mock_sync.call_args
        assert isinstance(args[0], io.BytesIO)

        # Verify save was called
        document_processor._save_document_content.assert_called_once()
        args, _ = document_processor._save_document_content.call_args
        assert args[1] == filename

@pytest.mark.asyncio
async def test_process_word_with_streams(document_processor):
    docx_bytes = create_docx_bytes()
    filename = "test.docx"
    user_id = 123
    file_hash = "fakehash"

    # Spy on _process_word_sync to ensure it receives BytesIO
    with patch.object(document_processor, "_process_word_sync", wraps=document_processor._process_word_sync) as mock_sync:
        result = await document_processor._process_word(docx_bytes, filename, user_id, file_hash)

        assert result["success"] is True
        assert result["content"].strip() == "Hello World"

        # Verify _process_word_sync was called with BytesIO
        args, _ = mock_sync.call_args
        assert isinstance(args[0], io.BytesIO)

        # Verify save was called
        document_processor._save_document_content.assert_called_once()
        args, _ = document_processor._save_document_content.call_args
        assert args[1] == filename

@pytest.mark.asyncio
async def test_write_temp_file_sync_not_called(document_processor):
    # Ensure _write_temp_file_sync is NOT called
    document_processor._write_temp_file_sync = MagicMock()

    pdf_bytes = create_pdf_bytes()
    await document_processor._process_pdf(pdf_bytes, "test.pdf", 123, "hash")
    document_processor._write_temp_file_sync.assert_not_called()

    docx_bytes = create_docx_bytes()
    await document_processor._process_word(docx_bytes, "test.docx", 123, "hash")
    document_processor._write_temp_file_sync.assert_not_called()
