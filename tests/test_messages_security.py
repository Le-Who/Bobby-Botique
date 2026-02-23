import pytest
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from app.exceptions import InputSanitizationError

# Create mocks
mock_security = MagicMock()
mock_doc_processor = MagicMock()
# Mock document_processor.document_processor (the instance inside the module)
mock_doc_processor.document_processor = MagicMock()
mock_doc_processor.document_processor.get_user_document_stats = AsyncMock(return_value={"document_count": 1, "limit_reached": False})

# Mock formatter
mock_formatter_class = MagicMock()
mock_formatter_class.format_text.return_value = ("Formatted text", "Markdown")

# Mock metrics
mock_metrics_module = MagicMock()
mock_metrics_module.metrics_collector = MagicMock()
mock_metrics_module.metrics_collector.record_api_call = AsyncMock()
mock_metrics_module.metrics_collector.record_error = AsyncMock()

# Mock the modules in sys.modules
with patch.dict("sys.modules", {
    "app.security": mock_security,
    "app.document_processor": mock_doc_processor,
    "app.metrics": mock_metrics_module,
    "app.utils.formatting": MagicMock(TelegramFormatter=mock_formatter_class),
    "app.utils.api_logger": MagicMock(),
    "app.prompts": MagicMock(),
    "app.handlers.agent": MagicMock(),
    "app.state": MagicMock(),
    "app.handlers.menus": MagicMock(),
    "app.request_context": MagicMock(),
    "app.tracing": MagicMock(),
    "app.database": MagicMock(),
    "app.config": MagicMock(),
}):
    # Reload app.handlers.messages if it was already imported
    if "app.handlers.messages" in sys.modules:
        del sys.modules["app.handlers.messages"]

    from app.handlers import messages

@pytest.mark.asyncio
async def test_handle_document_validation_success():
    """Test that handle_document calls validate_file_upload and proceeds on success."""
    update = MagicMock()
    context = MagicMock()
    document = MagicMock()
    document.file_name = "test.pdf"
    document.file_size = 1024
    document.mime_type = "application/pdf"
    document.get_file = AsyncMock()
    update.message.document = document
    update.effective_user.id = 123
    update.effective_chat.id = 456

    # Ensure reply_text returns a mock that has awaitable edit_text
    processing_msg_mock = MagicMock()
    processing_msg_mock.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=processing_msg_mock)

    # Mock validate_file_upload
    messages.validate_file_upload.reset_mock()
    messages.validate_file_upload.return_value = True

    # Mock process_uploaded_document
    messages.process_uploaded_document.reset_mock()
    messages.process_uploaded_document = AsyncMock(return_value={"success": True, "pages": 1, "text_length": 100})

    # Mock local import of document_processor inside handle_document
    mock_doc_processor.document_processor.get_user_document_stats = AsyncMock(return_value={"document_count": 1, "limit_reached": False})

    await messages.handle_document(update, context)

    # Verify validation was called
    messages.validate_file_upload.assert_called_once_with("test.pdf", 1024, "application/pdf")
    # Verify processing proceeded
    messages.process_uploaded_document.assert_called_once()
    # Verify success message was formatted (implying success path taken)
    mock_formatter_class.format_text.assert_called()

@pytest.mark.asyncio
async def test_handle_document_validation_failure():
    """Test that handle_document catches InputSanitizationError and replies with error."""
    update = MagicMock()
    context = MagicMock()
    document = MagicMock()
    document.file_name = "malicious.exe"
    document.file_size = 1024
    document.mime_type = "application/x-msdownload"
    update.message.document = document
    update.effective_user.id = 123

    # Ensure reply_text is awaitable
    update.message.reply_text = AsyncMock()

    # Mock validate_file_upload to raise exception
    messages.validate_file_upload.reset_mock()
    messages.validate_file_upload.side_effect = InputSanitizationError("Invalid file extension")

    await messages.handle_document(update, context)

    # Verify validation was called
    messages.validate_file_upload.assert_called_once()
    # Verify error message sent
    update.message.reply_text.assert_called_with("❌ Ошибка валидации файла: Invalid file extension")
