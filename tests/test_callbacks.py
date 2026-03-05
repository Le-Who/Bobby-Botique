# ruff: noqa: E402
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

# Mock all dependencies (executed only during module execution, not collection)
_mock_keys = [
    "telegram",
    "telegram.ext",
    "app.database",
    "app.config",
    "app.state",
    "app.metrics",
    "app.handlers.agent",
    "app.handlers.menus",
    "app.document_processor",
    "app.prompts",
    "app.utils.formatting",
    "app.utils.decorators",
    "app.errors",
]

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


async def async_test_document_callback_structure():
    from app.handlers import cb_documents

    """Verify that document_callback exists and can be called."""
    assert hasattr(cb_documents, "document_callback")

    # Create mocks
    update = MagicMock()
    context = MagicMock()
    query = MagicMock()

    update.callback_query = query
    query.data = "doc:list"
    query.from_user.id = 12345
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    # Mock the menus module used by cb_documents
    cb_documents.menus.get_documents_menu_content = AsyncMock(return_value=("text", "mode", "markup"))

    # Call the function
    await cb_documents.document_callback(update, context)

    # Assertions
    query.answer.assert_called()
    cb_documents.menus.get_documents_menu_content.assert_called_with(12345)
    query.edit_message_text.assert_called()


def test_document_callback_structure():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_test_document_callback_structure())
    loop.close()


if __name__ == "__main__":
    test_document_callback_structure()
