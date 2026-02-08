import pytest
import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Mock all dependencies
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()
sys.modules['app.database'] = MagicMock()
sys.modules['app.config'] = MagicMock()
sys.modules['app.state'] = MagicMock()
sys.modules['app.metrics'] = MagicMock()
sys.modules['app.handlers.agent'] = MagicMock()
sys.modules['app.handlers.menus'] = MagicMock()
sys.modules['app.document_processor'] = MagicMock()
sys.modules['app.prompts'] = MagicMock()
sys.modules['app.utils.formatting'] = MagicMock()
sys.modules['app.utils.decorators'] = MagicMock()
sys.modules['app.errors'] = MagicMock()

# Now import
from app.handlers import callbacks

async def async_test_document_callback_structure():
    """Verify that document_callback exists and can be called."""
    assert hasattr(callbacks, 'document_callback')

    # Create mocks
    update = MagicMock()
    context = MagicMock()
    query = MagicMock()

    update.callback_query = query
    query.data = "doc:list"
    query.from_user.id = 12345
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    # Since we mocked app.handlers.menus above, we need to set its methods
    # BUT, because we mocked it as a module, `callbacks.menus` will be that mock.
    # However, `from app.handlers import menus` in callbacks.py might resolve to the mock.

    callbacks.menus.get_documents_menu_content = AsyncMock(return_value=("text", "mode", "markup"))

    # Call the function
    await callbacks.document_callback(update, context)

    # Assertions
    query.answer.assert_called()
    callbacks.menus.get_documents_menu_content.assert_called_with(12345)
    query.edit_message_text.assert_called()

def test_document_callback_structure():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_test_document_callback_structure())
    loop.close()

if __name__ == "__main__":
    test_document_callback_structure()
