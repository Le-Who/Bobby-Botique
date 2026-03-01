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
from app.errors import DecryptionError


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
        fallback_priority=["gemini-2.5-pro"],
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
        fallback_priority=["gemini-2.5-pro"],
        provider_name="Gemini",
    )

    assert key is None
    assert model is None
    assert resolution == "decryption_failed"
    assert get_key.await_count == 2


@pytest.mark.asyncio
async def test_get_ai_response_with_key_rotation_decryption_message(use_case):
    """User sees a friendly message (not traceback) when DecryptionError occurs."""
    with patch(
        "app.ai_provider.ProviderRouter.get_response",
        new_callable=AsyncMock,
        return_value=(
            "\U0001f510 Ошибка расшифровки API-ключей. Обратитесь к администратору (возможно, изменился ADMIN_SECRET).",
            None,
        ),
    ):
        text, token_count = await use_case.get_ai_response_with_key_rotation(
            preferred_model="gemini-2.5-flash",
            history=[],
        )

    assert token_count is None
    assert "\U0001f510" in text  # 🔐
    assert "ADMIN_SECRET" in text
    # Must NOT contain Python traceback indicators
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
        fallback_priority=["gemini-2.5-pro"],
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
        fallback_priority=["gemini-2.5-pro", "gemini-2.5-flash-lite"],
        provider_name="Gemini",
    )

    # Only 1 call — immediately returned, no retries or fallback attempts
    assert get_key.await_count == 1
    assert resolution == "decryption_failed"
