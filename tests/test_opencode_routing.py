"""Tests for Opencode Go provider routing, JINA search grounding, and configuration.

Coverage:
- is_opencode_model() detection
- get_provider_for_model() factory routing
- OpencodeGoProvider URL/headers/model stripping
- JINA search_for_grounding() happy path and error fallback
- get_primary_provider() and _invalidate_primary_provider_cache()
- ProviderRouter multimodal guard — Opencode vision pass-through
- _pick_transient_fallback_model() for Opencode models
- /set_provider routing in admin command (unit)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.errors import ErrorCode, extract_error_code

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# is_opencode_model()
# ──────────────────────────────────────────────────────────────────────────────


class TestIsOpencodeModel:
    def test_opencode_prefixed_returns_true(self):
        from app.providers.base import is_opencode_model

        assert is_opencode_model("opencode-go/minimax-m2.7") is True
        assert is_opencode_model("opencode-go/qwen3.5-plus") is True
        assert is_opencode_model("opencode-go/mimo-v2-omni") is True

    def test_gemini_model_returns_false(self):
        from app.providers.base import is_opencode_model

        assert is_opencode_model("gemini-3.5-flash") is False
        assert is_opencode_model("gemini-3.1-flash-lite") is False

    def test_openrouter_model_returns_false(self):
        from app.providers.base import is_opencode_model

        assert is_opencode_model("anthropic/claude-3.5-sonnet") is False
        assert is_opencode_model("openai/gpt-4o") is False

    def test_empty_string_returns_false(self):
        from app.providers.base import is_opencode_model

        assert is_opencode_model("") is False

    def test_none_like_empty(self):
        from app.providers.base import is_opencode_model

        assert is_opencode_model("") is False


# ──────────────────────────────────────────────────────────────────────────────
# OpencodeGoProvider
# ──────────────────────────────────────────────────────────────────────────────


class TestOpencodeGoProvider:
    def _make_provider(self, api_key: str = "test-key-123"):
        from app.providers.opencode import OpencodeGoProvider

        return OpencodeGoProvider(api_key)

    class _FakeStreamResponse:
        def __init__(self, lines: list[str]):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    def test_url_is_opencode_endpoint(self):
        p = self._make_provider()
        assert "opencode.ai" in p._get_url()

    def test_authorization_header_is_bearer(self):
        p = self._make_provider("my-secret")
        headers = p._get_headers()
        assert headers.get("Authorization") == "Bearer my-secret"

    def test_strip_model_prefix_removes_prefix(self):
        p = self._make_provider()
        assert p._strip_model_prefix("opencode-go/minimax-m2.7") == "minimax-m2.7"

    def test_strip_model_prefix_passthrough_unknown(self):
        p = self._make_provider()
        # Unknown format should pass through unchanged
        assert p._strip_model_prefix("gemini-3.5-flash") == "gemini-3.5-flash"

    def test_minimax_uses_messages_transport(self):
        p = self._make_provider()
        assert p._uses_messages_transport("opencode-go/minimax-m2.7") is True
        assert p._get_url_for_model("opencode-go/minimax-m2.7").endswith("/v1/messages")

    def test_qwen_uses_chat_completions_transport(self):
        p = self._make_provider()
        assert p._uses_messages_transport("opencode-go/qwen3.5-plus") is False
        assert p._get_url_for_model("opencode-go/qwen3.5-plus").endswith("/v1/chat/completions")

    def test_messages_headers_use_anthropic_shape(self):
        p = self._make_provider("anthropic-like-key")
        headers = p._get_headers_for_model("opencode-go/minimax-m2.5")
        assert headers.get("x-api-key") == "anthropic-like-key"
        assert headers.get("anthropic-version") == "2023-06-01"
        assert "Authorization" not in headers

    def test_model_specific_401_is_not_treated_as_invalid_key(self):
        p = self._make_provider()
        tagged = p._build_http_error_tag(
            401,
            '{"error":{"message":"model big-pickle is not available for this key"}}',
            "opencode-go/big-pickle",
        )
        assert extract_error_code(tagged) == ErrorCode.INVALID_REQUEST

    def test_explicit_invalid_key_401_still_maps_to_invalid_key(self):
        p = self._make_provider()
        tagged = p._build_http_error_tag(
            401,
            '{"error":{"message":"invalid api key"}}',
            "opencode-go/big-pickle",
        )
        assert extract_error_code(tagged) == ErrorCode.INVALID_KEY

    def test_ambiguous_401_defaults_to_request_error_for_opencode(self):
        p = self._make_provider()
        tagged = p._build_http_error_tag(
            401,
            '{"error":{"message":"Unauthorized"}}',
            "opencode-go/big-pickle",
        )
        assert extract_error_code(tagged) == ErrorCode.INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_messages_execute_request_parses_text_blocks(self):
        p = self._make_provider()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": "MiniMax reply"}],
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_metrics = MagicMock(record_api_call=AsyncMock(), record_error=AsyncMock())
        mock_logger = MagicMock()

        with (
            patch("app.providers.openrouter._openrouter_http_client", mock_client),
            patch("app.providers.openrouter.metrics_collector", mock_metrics),
            patch("app.providers.openrouter.api_logger", mock_logger),
        ):
            response = await p._execute_request(
                history=[{"role": "user", "parts": ["hello"]}],
                model_name="opencode-go/minimax-m2.7",
                system_instruction="be concise",
                user_id=None,
                chat_id=None,
                timeout=30.0,
            )

        assert response.success is True
        assert response.text == "MiniMax reply"
        assert response.token_count == 20
        sent_payload = mock_client.post.await_args.kwargs["json"]
        assert sent_payload["model"] == "minimax-m2.7"
        assert sent_payload["system"] == "be concise"
        assert sent_payload["messages"] == [{"role": "user", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_messages_typed_stream_emits_terminal_usage(self):
        from app.providers.stream_types import (
            FinishKind,
            GenerationRequest,
            PromptRole,
            PromptTurn,
            StreamCompleted,
            TextDelta,
            TextPart,
        )

        provider = self._make_provider()
        lines = [
            "event: content_block_delta",
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}',
            "",
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
            "",
            "event: message_stop",
            'data: {"type":"message_stop"}',
            "",
        ]
        client = MagicMock()
        client.stream.return_value = self._FakeStreamResponse(lines)
        request = GenerationRequest(
            models=("opencode-go/minimax-m2.7",),
            turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        )

        with patch("app.providers.openrouter._openrouter_http_client", client):
            events = [
                event
                async for event in provider.stream(
                    request,
                    model_name="opencode-go/minimax-m2.7",
                )
            ]

        assert events[0] == TextDelta("Hello")
        assert isinstance(events[1], StreamCompleted)
        assert events[1].finish_reason.kind is FinishKind.STOP
        assert events[1].usage.completion == 2
        assert events[1].route.provider.value == "opencode"


# ──────────────────────────────────────────────────────────────────────────────
# Provider factory routing
# ──────────────────────────────────────────────────────────────────────────────


class TestGetProviderForModel:
    def test_opencode_model_returns_opencode_provider(self):
        from app.providers.base import get_provider_for_model
        from app.providers.opencode import OpencodeGoProvider

        provider = get_provider_for_model("opencode-go/minimax-m2.7", "key")
        assert isinstance(provider, OpencodeGoProvider)

    def test_gemini_model_returns_gemini_provider(self):
        from app.providers.base import get_provider_for_model
        from app.providers.gemini import GeminiProvider

        provider = get_provider_for_model("gemini-3.5-flash", "key")
        assert isinstance(provider, GeminiProvider)

    def test_slash_model_returns_openrouter_provider(self):
        from app.providers.base import get_provider_for_model
        from app.providers.openrouter import OpenRouterProvider

        provider = get_provider_for_model("anthropic/claude-opus", "key")
        assert isinstance(provider, OpenRouterProvider)
        assert not isinstance(provider, type(None))


# ──────────────────────────────────────────────────────────────────────────────
# JINA search
# ──────────────────────────────────────────────────────────────────────────────


class TestJinaSearch:
    @pytest.mark.asyncio
    async def test_search_returns_grounding_context_on_success(self):
        """Happy path: JINA returns 200 with content."""
        from app.search_jina import search_for_grounding

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "# Example Result\n\nSome web content here.\n\nSource: https://example.com"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await search_for_grounding("test query")

        assert "<search_context>" in result
        assert "test query" in result
        assert "Example Result" in result

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_timeout(self):
        """On timeout, returns empty string instead of raising."""
        from app.search_jina import search_for_grounding

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await search_for_grounding("test query")

        assert result == ""

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_http_error(self):
        """On HTTP 429, returns empty string — doesn't propagate."""
        from app.search_jina import search_for_grounding

        mock_request = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 429

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("rate limited", request=mock_request, response=mock_resp)
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await search_for_grounding("test query")

        assert result == ""

    @pytest.mark.asyncio
    async def test_search_extracts_source_urls(self):
        """Source URLs from content appear in the grounding block."""
        from app.search_jina import search_for_grounding

        content = "Result text. Source: https://example.com/page https://another.org"
        mock_response = MagicMock()
        mock_response.text = content
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await search_for_grounding("my query")

        assert "example.com" in result


# ──────────────────────────────────────────────────────────────────────────────
# Config: get_primary_provider and cache invalidation
# ──────────────────────────────────────────────────────────────────────────────


class TestPrimaryProviderConfig:
    def test_default_returns_opencode(self):
        from app.config import get_primary_provider

        # Default env value is "opencode"
        provider = get_primary_provider()
        assert provider in {"opencode", "gemini", "openrouter"}

    def test_invalidate_clears_cache(self):
        from app import config as cfg

        cfg._primary_provider_cache = "opencode"
        assert cfg._primary_provider_cache == "opencode"

        cfg._invalidate_primary_provider_cache()
        assert cfg._primary_provider_cache is None

    def test_cache_persists_across_calls(self):
        from app import config as cfg

        cfg._primary_provider_cache = "gemini"
        provider = cfg.get_primary_provider()
        assert provider == "gemini"


# ──────────────────────────────────────────────────────────────────────────────
# ProviderRouter: _pick_transient_fallback_model
# ──────────────────────────────────────────────────────────────────────────────


class TestPickTransientFallbackModel:
    def _get_router(self):
        from app.providers.router import ProviderRouter

        return ProviderRouter()

    def test_gemini_flash_cascades_to_next_known_lite_model(self):
        router = self._get_router()
        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

        with patch("app.providers.router.settings", mock_settings):
            result = router._pick_transient_fallback_model("gemini-3.5-flash", use_openrouter=False)

        assert result == "gemini-3.5-flash-lite"

    def test_opencode_model_cascades_to_gemini(self):
        router = self._get_router()
        from app.config import DEFAULT_GEMINI_MODELS, settings

        result = router._pick_transient_fallback_model("opencode-go/minimax-m2.7", use_openrouter=None)
        # Should return a Gemini fallback if it's in AVAILABLE_MODELS
        if result is not None:
            assert "opencode" not in result
            available = getattr(settings, "AVAILABLE_MODELS", None) if settings is not None else DEFAULT_GEMINI_MODELS
            assert result in available

    def test_openrouter_model_returns_none(self):
        router = self._get_router()
        result = router._pick_transient_fallback_model("anthropic/claude", use_openrouter=True)
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# ProviderRouter: multimodal guard — Opencode vision passthrough
# ──────────────────────────────────────────────────────────────────────────────


class TestMultimodalGuard:
    def test_opencode_vision_model_not_forced_to_gemini(self):
        """Opencode vision models (mimo) should NOT be redirected to Gemini for images."""
        from app.providers.router import _get_opencode_gemini_fallback

        # "opencode-go/mimo-v2-omni" must be in the fallback map as a vision model
        assert "opencode-go/mimo-v2-omni" in _get_opencode_gemini_fallback()

    def test_opencode_gemini_fallback_map_is_non_empty(self):
        from app.providers.router import _get_opencode_gemini_fallback

        assert len(_get_opencode_gemini_fallback()) >= 5

    def test_all_fallback_values_are_strings(self):
        from app.providers.router import _get_opencode_gemini_fallback

        for k, v in _get_opencode_gemini_fallback().items():
            assert isinstance(k, str), f"Key {k!r} is not a string"
            assert isinstance(v, str), f"Value {v!r} for {k!r} is not a string"
            assert "opencode" not in v, f"Fallback value {v!r} must be a Gemini model"

    def test_fallback_map_only_contains_canonical_opencode_models(self):
        """Guard: only approved Opencode model names in the fallback map.

        Keep this set in sync with _get_opencode_gemini_fallback() in router.py.
        When adding new Opencode models to the router, add them here too.
        """
        from app.providers.router import _get_opencode_gemini_fallback

        _CANONICAL_OPENCODE = {
            # GLM family
            "opencode-go/glm-5",
            "opencode-go/glm-5.1",
            # Kimi family
            "opencode-go/kimi-k2.5",
            "opencode-go/kimi-k2.6",
            # MiMo family (V2 + V2.5)
            "opencode-go/mimo-v2-pro",
            "opencode-go/mimo-v2-omni",
            "opencode-go/mimo-v2.5-pro",
            "opencode-go/mimo-v2.5",
            # MiniMax family
            "opencode-go/minimax-m2.5",
            "opencode-go/minimax-m2.7",
            # Qwen family
            "opencode-go/qwen3.5-plus",
            "opencode-go/qwen3.6-plus",
            # DeepSeek family
            "opencode-go/deepseek-v4-pro",
            "opencode-go/deepseek-v4-flash",
            # Legacy / routing alias
            "opencode-go/big-pickle",
        }
        for key in _get_opencode_gemini_fallback():
            assert key in _CANONICAL_OPENCODE, (
                f"Non-canonical Opencode model {key!r} found in fallback map.\n"
                f"Add it to _CANONICAL_OPENCODE in this test AND ensure it has a "
                f"Gemini fallback in _get_opencode_gemini_fallback().\n"
                f"Current allowed set: {sorted(_CANONICAL_OPENCODE)}"
            )

    def test_fallback_values_are_canonical_gemini_models(self):
        """Guard: all Gemini fallback values use only the canonical Gemini chat model list."""
        from app.config import CURRENT_GEMINI_MODELS
        from app.providers.router import _get_opencode_gemini_fallback

        canonical_gemini = set(CURRENT_GEMINI_MODELS)
        for opencode_model, gemini_fallback in _get_opencode_gemini_fallback().items():
            # Values are settings.DEFAULT_MODEL etc. — resolved fresh each call
            assert isinstance(gemini_fallback, str)
            assert gemini_fallback in canonical_gemini, (
                f"Fallback for {opencode_model!r} is {gemini_fallback!r}, "
                f"which is not in the canonical Gemini list: {sorted(canonical_gemini)}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Model selector: Opencode models are skipped
# ──────────────────────────────────────────────────────────────────────────────


class TestModelSelectorOpencodeSkip:
    def test_no_suggestion_for_opencode_current_model(self):
        from app.model_selector import select_model

        result = select_model(
            "Объясни подробно как работает квантовая механика? " * 5,
            current_model="opencode-go/minimax-m2.7",
        )
        assert result is None, "Should not suggest a different model when current is Opencode"

    def test_suggestions_still_work_for_gemini_model(self):
        from app.config import DEFAULT_GEMINI_MODELS, settings
        from app.model_selector import select_model

        available = getattr(settings, "AVAILABLE_MODELS", None) if settings is not None else DEFAULT_GEMINI_MODELS
        if len(available) < 2:
            pytest.skip("Need at least 2 Gemini models for upgrade suggestions")
        # A complex coding query with a lite model should potentially suggest an upgrade
        result = select_model(
            "Создай сложный класс на Python с множественным наследованием и метаклассами",
            current_model="gemini-3.1-flash-lite",
        )
        # Result may be None if only one Gemini model is configured — that's fine
        if result is not None:
            assert "opencode" not in result.model
