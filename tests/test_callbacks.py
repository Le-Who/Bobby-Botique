# ruff: noqa: E402
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Isolate in dedicated xdist worker — setup_module mutates sys.modules.
pytestmark = pytest.mark.xdist_group("sys_modules_isolation")

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
    _original_modules["__app_keys_before__"] = {k for k in sys.modules if k.startswith("app.")}
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()


def teardown_module(module):
    # 1. Restore original entries
    app_keys_before = _original_modules.pop("__app_keys_before__", set())
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)

    # 2. Purge any app.* modules imported DURING the mocked period.
    #    They hold stale `settings = MagicMock()` bindings that cannot be
    #    fixed by restoring sys.modules alone.
    for k in list(sys.modules):
        if k.startswith("app.") and k not in app_keys_before:
            del sys.modules[k]


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
