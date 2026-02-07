import pytest
import os
import sys
from unittest.mock import MagicMock, AsyncMock

# Patching environment variables and sys.modules at module level
os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
os.environ["ADMIN_ID"] = "123456"
os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost/db"
os.environ["GEMINI_API_KEYS"] = "key1"
os.environ["TAVILY_API_KEYS"] = "key1"
os.environ["PORT"] = "10000"

# Mock database
sys.modules["app.database"] = MagicMock()

from app.document_processor import DocumentProcessor

@pytest.mark.asyncio
async def test_docx_magic_bytes_validation():
    processor = DocumentProcessor()

    # Mock internal methods
    processor._write_temp_file_sync = MagicMock(return_value="/tmp/fake.docx")
    processor._process_word_sync = MagicMock(return_value={"content": "fake content"})
    processor._save_document_content = AsyncMock()
    processor._check_document_limit = AsyncMock(return_value=True)
    processor._calculate_file_hash = MagicMock(return_value="hash")
    processor._check_duplicate_file = AsyncMock(return_value=None)
    processor._cleanup_oldest_documents = AsyncMock()

    # Invalid content
    invalid_content = b'this is not a zip file'

    result = await processor._process_word(
        file_data=invalid_content,
        filename="test.docx",
        user_id=123,
        file_hash="hash"
    )

    assert "error" in result
    assert result["error"] == "Invalid Word document format. File must be a valid .docx file."
