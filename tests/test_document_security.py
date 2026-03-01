# ruff: noqa: E402
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Patching environment variables and sys.modules at module level
os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
os.environ["ADMIN_ID"] = "123456"
os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost/db"
os.environ["GEMINI_API_KEYS"] = "key1"
os.environ["TAVILY_API_KEYS"] = "key1"
os.environ["PORT"] = "10000"

_mock_keys = ["app.database"]
_original_modules = {}


def setup_module(module):
    global _original_modules
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()


def teardown_module(module):
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)


@pytest.mark.asyncio
async def test_docx_magic_bytes_validation():
    from app.document_processor import DocumentProcessor

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
    invalid_content = b"this is not a zip file"

    result = await processor._process_word_unified(
        file_data=invalid_content, filename="test.docx", user_id=123, file_hash="hash"
    )

    assert "error" in result
    assert (
        result["error"]
        == "Invalid Word document format. File must be a valid .docx file."
    )
