"""
Tests for DecryptionError handling in the key resolution path.

Verifies that:
1. _resolve_key_generic catches DecryptionError and returns 'decryption_failed'
2. get_ai_response_with_key_rotation shows a user-friendly message
3. DecryptionError in fallback models is also caught
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agent_use_cases import AgentRequestUseCase
from app.errors import DecryptionError, ErrorCode, extract_error_code


@pytest.fixture
def use_case():
    return AgentRequestUseCase()


@pytest.mark.asyncio
async def test_resolve_key_generic_catches_decryption_error(use_case):
    """DecryptionError from get_key_func returns 'decryption_failed' resolution."""
    get_key = AsyncMock(side_effect=DecryptionError("bad ADMIN_SECRET"))

    key, model, resolution = await use_case._resolve_key_generic(
        preferred_model="gemini-2.5-flash",
        get_key_func=get_key,
        fallback_priority=["gemini-2.5-flash"],
        provider_name="Gemini",
    )

    assert key is None
    assert model is None
    assert resolution == "decryption_failed"
    get_key.assert_awaited_once_with("gemini-2.5-flash", excluded_hashes=set())


@pytest.mark.asyncio
async def test_resolve_key_generic_catches_decryption_error_in_fallback(use_case):
    """DecryptionError during fallback model resolution also returns 'decryption_failed'."""
    # First call: returns None (no key for preferred model)
    # Second call (fallback): raises DecryptionError
    get_key = AsyncMock(side_effect=[None, DecryptionError("bad secret")])

    key, model, resolution = await use_case._resolve_key_generic(
        preferred_model="gemini-2.5-flash",
        get_key_func=get_key,
        fallback_priority=["gemini-3.1-flash-lite"],
        provider_name="Gemini",
    )

    assert key is None
    assert model is None
    assert resolution == "decryption_failed"
    assert get_key.await_count == 2


@pytest.mark.asyncio
async def test_get_ai_response_with_key_rotation_decryption_message(use_case):
    """The real router renders a safe tagged message when key resolution fails."""
    from app.providers.router import ProviderRouter

    with (
        patch("app.providers.get_provider_router", return_value=ProviderRouter()),
        patch.object(
            AgentRequestUseCase, "resolve_ai_request", new=AsyncMock(return_value=(None, None, "decryption_failed"))
        ) as resolve,
        patch.object(AgentRequestUseCase, "get_ai_response", new=AsyncMock()) as provider,
    ):
        text, token_count = await use_case.get_ai_response_with_key_rotation(
            preferred_model="gemini-2.5-flash", history=[]
        )

    resolve.assert_awaited_once_with("gemini-2.5-flash", use_openrouter=None, excluded_key_hashes=set())
    provider.assert_not_awaited()
    assert extract_error_code(text) is ErrorCode.DECRYPTION_FAILED
    assert token_count is None
    assert "администратор" in text
    assert "ADMIN_SECRET" not in text
    assert "API" not in text
    assert "Traceback" not in text
    assert "raise " not in text


@pytest.mark.asyncio
async def test_normal_key_resolution_unaffected(use_case):
    """Normal key resolution (no DecryptionError) still works correctly."""
    mock_key = {"key_hash": "abc123", "api_key": "decrypted-key"}
    get_key = AsyncMock(return_value=mock_key)

    key, model, resolution = await use_case._resolve_key_generic(
        preferred_model="gemini-2.5-flash",
        get_key_func=get_key,
        fallback_priority=["gemini-2.5-flash"],
        provider_name="Gemini",
    )

    assert key == mock_key
    assert model == "gemini-2.5-flash"
    assert resolution is None


@pytest.mark.asyncio
async def test_decryption_error_does_not_retry(use_case):
    """DecryptionError exits immediately, does not attempt further retries or fallback."""
    get_key = AsyncMock(side_effect=DecryptionError("secret changed"))

    key, model, resolution = await use_case._resolve_key_generic(
        preferred_model="gemini-2.5-flash",
        get_key_func=get_key,
        fallback_priority=["gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
        provider_name="Gemini",
    )

    # Only 1 call — immediately returned, no retries or fallback attempts
    assert get_key.await_count == 1
    assert resolution == "decryption_failed"
