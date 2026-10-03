"""Tests for document I/O handlers: PDF, Word processing via physical file paths."""

import io
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image


@pytest.mark.asyncio
@patch("app.document_processor.DocumentProcessor._process_pdf_unified")
async def test_process_pdf_downloads_to_drive(mock_process_path, tmp_path):
    from app.document_processor import DocumentProcessor

    temp_path = tmp_path / "test.pdf"
    temp_path.touch()

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
        result = await processor.process_document(str(temp_path), "test.pdf", 1, is_path=True)

    mock_process_path.assert_called_once()
    args, _ = mock_process_path.call_args
    assert args[0] == str(temp_path)
    assert result == "Extracted PDF content"


@pytest.mark.asyncio
@patch("app.document_processor.DocumentProcessor._process_word_unified")
async def test_process_word_downloads_to_drive(mock_process_path, tmp_path):
    from app.document_processor import DocumentProcessor

    temp_path = tmp_path / "test.docx"
    temp_path.touch()

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
        result = await processor.process_document(str(temp_path), "test.docx", 1, is_path=True)

    mock_process_path.assert_called_once()
    args, _ = mock_process_path.call_args
    assert args[0] == str(temp_path)
    assert result == "Extracted DOCX content"


@pytest.mark.asyncio
async def test_save_image_as_bytes_uses_executor(monkeypatch):
    from app.utils import image_utils

    with Image.new("RGBA", (32, 16), (255, 0, 0, 255)) as image, io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        raw_image_data = buffer.getvalue()
    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(pool, "submit", wraps=pool.submit) as submit:
        monkeypatch.setattr(image_utils, "_image_process_pool", pool)
        result = await image_utils.save_image_as_bytes(raw_image_data)
        submit.assert_called_once_with(image_utils._image_worker, raw_image_data, 10, "default")

    assert isinstance(result, bytes)
    with Image.open(io.BytesIO(result)) as output:
        assert output.format == "JPEG"
        assert output.size == (32, 16)
        assert output.mode == "RGB"


@pytest.mark.asyncio
async def test_save_image_as_bytes_returns_none_on_executor_failure(monkeypatch):
    from app.utils import image_utils

    def fail_submit(*_args, **_kwargs):
        raise RuntimeError("executor unavailable")

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(image_utils, "_image_process_pool", pool)
        monkeypatch.setattr(pool, "submit", fail_submit)
        assert await image_utils.save_image_as_bytes(b"image input") is None
