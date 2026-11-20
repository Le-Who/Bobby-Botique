import pytest

from app.document_processor import DocumentProcessor


@pytest.mark.asyncio
async def test_docx_magic_bytes_validation():
    processor = DocumentProcessor()

    invalid_content = b"this is not a zip file"

    result = await processor._process_word_unified(file_data=invalid_content, filename="test.docx")

    assert "error" in result
    assert result["error"] == "Invalid Word document format. File must be a valid .docx file."
