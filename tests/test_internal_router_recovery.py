"""Background callers retain actionable transient failure provenance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import ErrorCode, extract_error_code, tag_error
from app.providers.router import ProviderRouter


def install_router(monkeypatch, responses, resolutions):
    use_case = SimpleNamespace(
        resolve_ai_request=AsyncMock(side_effect=resolutions),
        reserve_key_usage=AsyncMock(return_value=True),
        get_ai_response=AsyncMock(side_effect=responses),
    )
    health = SimpleNamespace(suspend_key=AsyncMock(), record_success=AsyncMock())
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    router = ProviderRouter()
    monkeypatch.setattr(router, "_pick_transient_fallback_model", lambda *args: None)
    return router, use_case


def key(model="gemini-3.5-flash-lite"):
    return {"api_key": "fake", "key_hash": "fake-hash"}, model, None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [ErrorCode.TIMEOUT, ErrorCode.NETWORK, ErrorCode.OVERLOADED])
@pytest.mark.parametrize("empty_after_failure", [True, False])
async def test_background_preserves_transient_when_attempts_or_eligible_keys_end(
    monkeypatch, code, empty_after_failure
):
    failure = tag_error(code, "temporary")
    resolutions = [key(), (None, None, "all_exhausted")]
    router, use_case = install_router(monkeypatch, [(failure, None)], resolutions)
    result, _ = await router.get_response(
        "gemini-3.5-flash-lite",
        [],
        max_key_retries=2 if empty_after_failure else 1,
        preserve_transient_errors=True,
    )
    assert extract_error_code(result) == code
    use_case.get_ai_response.assert_awaited_once()
    use_case.reserve_key_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_keeps_primary_transient_if_fallback_pool_is_temporarily_empty(monkeypatch):
    failure = tag_error(ErrorCode.OVERLOADED, "temporary")
    router, _ = install_router(monkeypatch, [(failure, None)], [key(), (None, None, "all_exhausted")])
    monkeypatch.setattr(router, "_pick_transient_fallback_model", lambda *args: "gemini-3.6-flash")
    result, _ = await router.get_response(
        "gemini-3.5-flash-lite", [], max_key_retries=1, preserve_transient_errors=True
    )
    assert extract_error_code(result) == ErrorCode.OVERLOADED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution,expected", [("no_keys", ErrorCode.NO_KEYS), ("all_exhausted", ErrorCode.KEYS_EXHAUSTED)]
)
async def test_initial_missing_keys_are_not_reclassified(monkeypatch, resolution, expected):
    router, use_case = install_router(monkeypatch, [], [(None, None, resolution)])
    result, _ = await router.get_response("gemini-3.5-flash-lite", [], preserve_transient_errors=True)
    assert extract_error_code(result) == expected
    use_case.get_ai_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_quota_failure_is_not_hidden_by_preceding_transient(monkeypatch):
    router, _ = install_router(
        monkeypatch,
        [
            (tag_error(ErrorCode.TIMEOUT, "temporary"), None),
            (tag_error(ErrorCode.QUOTA_EXCEEDED, "quota"), None),
        ],
        [key(), key()],
    )
    result, _ = await router.get_response(
        "gemini-3.5-flash-lite", [], max_key_retries=2, preserve_transient_errors=True
    )
    assert extract_error_code(result) == ErrorCode.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_fallback_quota_is_not_hidden_by_primary_transient(monkeypatch):
    router, _ = install_router(
        monkeypatch,
        [
            (tag_error(ErrorCode.TIMEOUT, "temporary"), None),
            (tag_error(ErrorCode.QUOTA_EXCEEDED, "quota"), None),
        ],
        [key(), key("gemini-3.6-flash"), (None, None, "all_exhausted")],
    )
    monkeypatch.setattr(router, "_pick_transient_fallback_model", lambda *args: "gemini-3.6-flash")
    result, _ = await router.get_response(
        "gemini-3.5-flash-lite", [], max_key_retries=1, preserve_transient_errors=True
    )
    assert extract_error_code(result) == ErrorCode.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_legacy_default_retains_exhausted_result(monkeypatch):
    router, _ = install_router(monkeypatch, [(tag_error(ErrorCode.TIMEOUT, "temporary"), None)], [key()])
    result, _ = await router.get_response("gemini-3.5-flash-lite", [], max_key_retries=1)
    assert extract_error_code(result) == ErrorCode.KEYS_EXHAUSTED
