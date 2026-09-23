from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import multimodal_processor


@pytest.mark.asyncio
async def test_explicit_media_key_uses_full_database_hash_for_health_updates() -> None:
    status = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    with (
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch("app.utils.multimodal_processor.get_cached_genai_client", return_value=object()),
        patch("app.utils.multimodal_processor.run_with_resilience", new=AsyncMock(return_value=("done", 1))),
    ):
        result = await multimodal_processor._generate_with_resilience(
            parts=[],
            model="gemini-3.6-flash",
            system_prompt="Describe",
            thinking_config=None,
            api_key="explicit-key",
        )

    assert result == "done"
    status.record_success.assert_awaited_once_with(hashlib.sha256(b"explicit-key").hexdigest(), "gemini-3.6-flash")
