from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.repos import memory_extraction


@pytest.mark.asyncio
async def test_extraction_rotates_full_database_key_hash_after_provider_failure() -> None:
    bad_hash = hashlib.sha256(b"bad-key").hexdigest()
    good_hash = hashlib.sha256(b"good-key").hexdigest()
    resolver = AsyncMock(return_value=({"api_key": "good-key", "key_hash": good_hash}, None, None))
    provider = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(side_effect=[RuntimeError("503 unavailable"), SimpleNamespace(text="{}")])
            )
        )
    )
    status = SimpleNamespace(suspend_key=AsyncMock(), record_success=AsyncMock())
    with (
        patch("app.handlers.ai_core._resolve_ai_request", resolver),
        patch("app.providers.gemini.get_cached_genai_client", return_value=provider),
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch("app.repos.memory_extraction.asyncio.sleep", new=AsyncMock()),
    ):
        result = await memory_extraction.extract_graph_structured("A personal fact worth remembering", "bad-key")

    assert result.entities == []
    assert resolver.await_args.kwargs["excluded_key_hashes"] == {bad_hash}
    assert status.suspend_key.await_args.args[0] == bad_hash
    assert status.record_success.await_args.args[0] == good_hash
