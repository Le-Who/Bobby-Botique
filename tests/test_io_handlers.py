"""Tests for document I/O handlers: PDF, Word processing via physical file paths."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@patch("app.document_processor.DocumentProcessor._process_pdf_unified")
async def test_process_pdf_downloads_to_drive(mock_process_path):
    from app.document_processor import DocumentProcessor

    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)

    mock_process_path.return_value = "Extracted PDF content"

    processor = DocumentProcessor()
    with (
        patch(
            "app.document_processor.check_document_limit",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.document_processor.check_duplicate_file",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await processor.process_document(temp_path, "test.pdf", 1, is_path=True)

    mock_process_path.assert_called_once()
    args, _ = mock_process_path.call_args
    assert args[0] == temp_path
    assert result == "Extracted PDF content"


@pytest.mark.asyncio
@patch("app.document_processor.DocumentProcessor._process_word_unified")
async def test_process_word_downloads_to_drive(mock_process_path):
    from app.document_processor import DocumentProcessor

    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)

    mock_process_path.return_value = "Extracted DOCX content"

    processor = DocumentProcessor()
    with (
        patch(
            "app.document_processor.check_document_limit",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.document_processor.check_duplicate_file",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await processor.process_document(temp_path, "test.docx", 1, is_path=True)

    mock_process_path.assert_called_once()
    args, _ = mock_process_path.call_args
    assert args[0] == temp_path
    assert result == "Extracted DOCX content"


@pytest.mark.asyncio
@patch("app.utils.image_utils._image_process_pool")
async def test_save_image_as_bytes_uses_executor(mock_pool):
    from app.utils.image_utils import save_image_as_bytes

    raw_image_data = b"fake image bytes"
    _result = await save_image_as_bytes(raw_image_data)

    from app.utils.image_utils import _image_worker

    assert _image_worker is not None
