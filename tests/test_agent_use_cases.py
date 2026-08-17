"""Tests for app.agent_use_cases — key resolution and fallback logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent_use_cases import AgentRequestUseCase, _gemini_fallback_priority


@pytest.fixture
def usecase():
    return AgentRequestUseCase()


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.DEFAULT_MODEL = "gemini-3.5-flash"
    s.QNA_MODEL = "gemini-3.1-flash-lite"
    s.RESEARCH_MODEL = "gemini-3.5-flash"
    s.URL_SELECTION_MODEL = "gemini-3.1-flash-lite"
    s.OPENROUTER_DEFAULT_MODEL = "stepfun/step-3.5-flash:free"
    s.OPENROUTER_QNA_MODEL = "stepfun/step-3.5-flash:free"
    s.OPENROUTER_RESEARCH_MODEL = "stepfun/step-3.5-flash:free"
    s.OPENROUTER_URL_SELECTION_MODEL = "stepfun/step-3.5-flash:free"
    return s


def test_dynamic_gemini_role_model_remains_eligible_for_key_fallback(mock_settings):
    mock_settings.AVAILABLE_MODELS = ["gemini-3.7-flash"]
    mock_settings.DEFAULT_MODEL = "gemini-3.7-flash"
    mock_settings.RESEARCH_MODEL = "gemini-3.7-flash"
    mock_settings.QNA_MODEL = "gemini-3.7-flash"
    mock_settings.INLINE_MODEL = "gemini-3.7-flash"

    with patch("app.agent_use_cases.settings", mock_settings):
        fallbacks = _gemini_fallback_priority("gemini-3.6-flash")

    assert "gemini-3.7-flash" in fallbacks


def test_custom_gemini_model_uses_full_known_fallback_chain_even_when_hidden(mock_settings):
    mock_settings.AVAILABLE_MODELS = ["gemini-3.7-flash"]
    mock_settings.DEFAULT_MODEL = "gemini-3.7-flash"
    mock_settings.RESEARCH_MODEL = "gemini-3.7-flash"
    mock_settings.QNA_MODEL = "gemini-3.7-flash"
    mock_settings.INLINE_MODEL = "gemini-3.7-flash"

    with patch("app.agent_use_cases.settings", mock_settings):
        fallbacks = _gemini_fallback_priority("gemini-3.7-flash")

    assert fallbacks[:4] == [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


# ── resolve_ai_request ───────────────────────────────────────────────────────


class TestResolveAiRequest:
    """Key resolution with fallback chain."""

    @pytest.mark.asyncio
    async def test_preferred_key_found(self, usecase, mock_settings):
        """When a key is available for the preferred model, return it directly."""
        fake_key = {"api_key": "key123", "key_hash": "hash123"}

        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch(
                "app.agent_use_cases.get_available_gemini_key",
                new_callable=AsyncMock,
                return_value=fake_key,
            ),
        ):
            key, model, status = await usecase.resolve_ai_request("gemini-3.5-flash")

        assert key == fake_key
        assert model == "gemini-3.5-flash"
        assert status is None

    @pytest.mark.asyncio
    async def test_fallback_when_preferred_exhausted(self, usecase, mock_settings):
        """When preferred model key is exhausted, fallback to next available."""
        fallback_key = {"api_key": "fallback_key", "key_hash": "fallback_hash"}

        async def mock_get_key(model, excluded_hashes=None):
            if model == "gemini-3.5-flash":
                return None  # Exhausted
            return fallback_key

        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch("app.agent_use_cases.get_available_gemini_key", side_effect=mock_get_key),
            patch("app.agent_use_cases.invalidate_key_cache", new_callable=AsyncMock),
        ):
            key, model, status = await usecase.resolve_ai_request("gemini-3.5-flash")

        assert key == fallback_key
        assert model == "gemini-3.5-flash-lite"
        assert status == "confirm_fallback"

    @pytest.mark.asyncio
    async def test_gemini_3_5_exhaustion_checks_complete_known_chain(self, usecase, mock_settings):
        """Hidden runtime fallbacks must still be checked before reaching 3.1 Flash Lite."""
        mock_settings.DEFAULT_MODEL = "gemini-3.5-flash"
        mock_settings.RESEARCH_MODEL = "gemini-3.5-flash"
        mock_settings.QNA_MODEL = "gemini-3.1-flash-lite"
        fallback_key = {"api_key": "lite_key", "key_hash": "lite_hash"}
        attempted_models: list[str] = []

        async def mock_get_key(model, excluded_hashes=None):
            attempted_models.append(model)
            if model == "gemini-3.1-flash-lite":
                return fallback_key
            return None

        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch("app.agent_use_cases.get_available_gemini_key", side_effect=mock_get_key),
            patch("app.agent_use_cases.invalidate_key_cache", new_callable=AsyncMock),
        ):
            key, model, status = await usecase.resolve_ai_request("gemini-3.5-flash")

        assert key == fallback_key
        assert model == "gemini-3.1-flash-lite"
        assert status == "confirm_fallback"
        assert attempted_models[:3] == [
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
        ]

    @pytest.mark.asyncio
    async def test_all_keys_exhausted(self, usecase, mock_settings):
        """When no keys are available at all, return all_exhausted status."""
        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch(
                "app.agent_use_cases.get_available_gemini_key",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.agent_use_cases.invalidate_key_cache", new_callable=AsyncMock),
        ):
            key, model, status = await usecase.resolve_ai_request("gemini-3.5-flash")

        assert key is None
        assert status == "all_exhausted"

    @pytest.mark.asyncio
    async def test_decryption_error(self, usecase, mock_settings):
        """DecryptionError should return decryption_failed status."""
        from app.errors import DecryptionError

        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch(
                "app.agent_use_cases.get_available_gemini_key",
                new_callable=AsyncMock,
                side_effect=DecryptionError("bad secret"),
            ),
        ):
            key, model, status = await usecase.resolve_ai_request("gemini-3.5-flash")

        assert key is None
        assert status == "decryption_failed"

    @pytest.mark.asyncio
    async def test_openrouter_no_keys(self, usecase, mock_settings):
        """OpenRouter model with no keys should return no_keys status."""
        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch("app.agent_use_cases.get_openrouter_keys", return_value=[]),
        ):
            key, model, status = await usecase.resolve_ai_request("anthropic/claude-3-sonnet")

        assert key is None
        assert status == "no_keys"

    @pytest.mark.asyncio
    async def test_slash_in_model_routes_to_openrouter(self, usecase, mock_settings):
        """Model name with '/' should be routed to OpenRouter."""
        fake_key = {"api_key": "or_key", "key_hash": "or_hash"}

        with (
            patch("app.agent_use_cases.settings", mock_settings),
            patch("app.agent_use_cases.get_openrouter_keys", return_value=["k1"]),
            patch(
                "app.agent_use_cases.get_available_openrouter_key",
                new_callable=AsyncMock,
                return_value=fake_key,
            ),
        ):
            key, model, status = await usecase.resolve_ai_request("anthropic/claude-3-sonnet")

        assert key == fake_key


# ── increment_key_usage ──────────────────────────────────────────────────────


class TestIncrementKeyUsage:
    """Key usage tracking dispatches to correct provider."""

    @pytest.mark.asyncio
    async def test_gemini_model_increments_gemini(self, usecase):
        with (
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch("app.agent_use_cases.increment_gemini_key_usage", new_callable=AsyncMock) as mock_inc,
        ):
            await usecase.increment_key_usage("hash1", "gemini-3.5-flash")
            mock_inc.assert_called_once_with("hash1", "gemini-3.5-flash")

    @pytest.mark.asyncio
    async def test_openrouter_model_increments_openrouter(self, usecase):
        with (
            patch("app.agent_use_cases.get_use_openrouter", return_value=False),
            patch(
                "app.agent_use_cases.increment_openrouter_key_usage",
                new_callable=AsyncMock,
            ) as mock_inc,
        ):
            await usecase.increment_key_usage("hash1", "anthropic/claude-3-sonnet")
            mock_inc.assert_called_once()
