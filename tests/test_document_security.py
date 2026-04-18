# ruff: noqa: E402
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Isolate in dedicated xdist worker — mutates sys.modules and os.environ in setup_module.
pytestmark = pytest.mark.xdist_group("sys_modules_isolation")

_mock_keys = ["app.database"]
_original_modules = {}
_original_env: dict[str, str | None] = {}

# Environment variables this test module requires
_ENV_OVERRIDES = {
    "TELEGRAM_BOT_TOKEN": "123:test",
    "ADMIN_ID": "123456",
    "DATABASE_URL": "postgresql://user:pass@localhost/db",
    "GEMINI_API_KEYS": "key1",
    "TAVILY_API_KEYS": "key1",
    "PORT": "10000",
}


def setup_module(module):
    global _original_modules, _original_env

    # Save and override environment variables
    _original_env.clear()
    for k, v in _ENV_OVERRIDES.items():
        _original_env[k] = os.environ.get(k)
        os.environ[k] = v

    _original_modules["__app_keys_before__"] = {k for k in sys.modules if k.startswith("app.")}
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()


def teardown_module(module):
    # Restore environment variables
    for k, orig in _original_env.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig

    app_keys_before = _original_modules.pop("__app_keys_before__", set())
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)
    for k in list(sys.modules):
        if k.startswith("app.") and k not in app_keys_before:
            del sys.modules[k]


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
    assert result["error"] == "Invalid Word document format. File must be a valid .docx file."
