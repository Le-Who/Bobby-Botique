import hashlib
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest


@dataclass
class _SeedSettings:
    ADMIN_ID: int = 123
    GEMINI_API_KEYS: list[str] = field(default_factory=lambda: ["gemini-current-1", "gemini-current-2"])
    TAVILY_API_KEYS: list[str] = field(default_factory=lambda: ["tavily-current"])
    OPENROUTER_API_KEYS: list[str] = field(default_factory=lambda: ["openrouter-current"])
    DAILY_LIMITS: dict[str, int] = field(default_factory=dict)


def _hashes(keys: list[str]) -> list[str]:
    return [hashlib.sha256(key.encode()).hexdigest() for key in keys]


@pytest.mark.asyncio
async def test_insert_initial_data_prunes_stale_key_rows():
    from app.db.seed import insert_initial_data

    settings = _SeedSettings()
    db_query = AsyncMock()
    db_execute_many = AsyncMock()

    with patch("app.crypto.encrypt_api_key", side_effect=lambda key: f"enc:{key}"):
        await insert_initial_data(db_query, db_execute_many, settings)

    query_calls = [(call.args[0], call.args[1] if len(call.args) > 1 else ()) for call in db_query.call_args_list]

    expected_gemini_hashes = _hashes(settings.GEMINI_API_KEYS)
    expected_tavily_hashes = _hashes(settings.TAVILY_API_KEYS)
    expected_openrouter_hashes = _hashes(settings.OPENROUTER_API_KEYS)

    assert any(
        "DELETE FROM key_model_status" in sql and "FROM api_keys" in sql and params == (expected_gemini_hashes,)
        for sql, params in query_calls
    )
    assert any(
        "DELETE FROM api_keys" in sql and "key_hash != ALL($1::text[])" in sql and params == (expected_gemini_hashes,)
        for sql, params in query_calls
    )
    assert any(
        "DELETE FROM tavily_api_keys" in sql
        and "key_hash != ALL($1::text[])" in sql
        and params == (expected_tavily_hashes,)
        for sql, params in query_calls
    )
    assert any(
        "DELETE FROM key_model_status" in sql
        and "FROM openrouter_api_keys" in sql
        and params == (expected_openrouter_hashes,)
        for sql, params in query_calls
    )
    assert any(
        "DELETE FROM openrouter_api_keys" in sql
        and "key_hash != ALL($1::text[])" in sql
        and params == (expected_openrouter_hashes,)
        for sql, params in query_calls
    )

    upsert_calls = [call.args for call in db_execute_many.call_args_list]
    assert any("INSERT INTO api_keys" in sql and len(data) == 2 for sql, data in upsert_calls)
    assert any("INSERT INTO tavily_api_keys" in sql and len(data) == 1 for sql, data in upsert_calls)
    assert any("INSERT INTO openrouter_api_keys" in sql and len(data) == 1 for sql, data in upsert_calls)
