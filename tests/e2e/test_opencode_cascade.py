"""
E2E tests for the Split-Brain Provider Cascade (Opencode Go → Gemini fallback).

Design decisions
────────────────
These tests drive ProviderRouter.get_response() directly — no Telegram layer.
All I/O is mocked at the boundary (AgentRequestUseCase + KeyStatusManager).
We use Fake objects instead of MagicMock to enable state-based assertions that
remain valid under internal refactoring.

Coverage:
  OC-01  Opencode keys exhausted → transparent Gemini fallback
  OC-02  Opencode streaming cascade → Gemini stream fallback
  OC-03  Gemini fallback exhausted → meaningful error surfaced
  OC-04  Opencode vision model (mimo-v2-omni) maps to gemini-3.5-flash
  OC-05  Non-Opencode model bypasses cascade entirely
  OC-06  _is_fallback=True prevents infinite recursion
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers import ProviderRouter

# ── Fake helpers ──────────────────────────────────────────────────────────────


class FakeKeyStatusManager:
    """In-memory KeyStatusManager that tracks suspended keys by state."""

    def __init__(self) -> None:
        self.suspended: dict[str, str] = {}  # key_hash → error_category
        self.successes: list[str] = []  # key_hashes recorded successful

    async def suspend_key(
        self,
        key_hash: str,
        model_name: str,
        error_category: str,
        error_text: str = "",
    ) -> None:
        self.suspended[key_hash] = error_category

    async def record_success(self, key_hash: str, model_name: str) -> None:
        self.successes.append(key_hash)


def _make_use_case(resolve_side_effects: list, response_side_effects: list):
    """Build a mock AgentRequestUseCase with controlled side-effects."""
    uc = MagicMock()
    uc.resolve_ai_request = AsyncMock(side_effect=resolve_side_effects)
    uc.get_ai_response = AsyncMock(side_effect=response_side_effects)
    uc.increment_key_usage = AsyncMock()
    return uc


# ── OC-01: get_response Opencode cascade ──────────────────────────────────────


class TestOpencodeGetResponseCascade:
    @pytest.mark.asyncio
    async def test_opencode_exhaustion_falls_back_to_gemini(self):
        """When all Opencode keys are exhausted on get_response,
        the router transparently retries with the mapped Gemini model.
        """
        fake_status = FakeKeyStatusManager()
        history = [{"role": "user", "parts": ["hello"]}]

        # First call (Opencode): keys exhausted immediately
        # Second call (Gemini fallback after re-entering get_response):
        resolve_effects = [
            (None, None, "all_exhausted"),  # opencode → exhausted
            ({"api_key": "gk1", "key_hash": "ghash1"}, "gemini-3.5-flash", None),  # gemini ok
        ]
        response_effects = [
            ("Fallback answer!", 42),
        ]

        use_case = _make_use_case(resolve_effects, response_effects)

        mock_settings = MagicMock()
        mock_settings.DEFAULT_MODEL = "gemini-3.5-flash"
        mock_settings.RESEARCH_MODEL = "gemini-3.5-flash"
        mock_settings.QNA_MODEL = "gemini-3.1-flash-lite"
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            router = ProviderRouter()
            text, tokens = await router.get_response(
                "opencode-go/minimax-m2.7",
                history,
                max_key_retries=1,
            )

        # State-based assertions: confirmed successful response from fallback
        assert text == "Fallback answer!"
        assert tokens == 42
        # The Gemini key should have been marked successful (not suspended)
        assert "ghash1" in fake_status.successes
        assert "ghash1" not in fake_status.suspended

    @pytest.mark.asyncio
    async def test_vision_model_maps_to_gemini_flash(self):
        """The vision-capable mimo-v2-omni should cascade to gemini-3.5-flash."""
        fake_status = FakeKeyStatusManager()
        history = [{"role": "user", "parts": ["describe image"]}]

        resolve_effects = [
            (None, None, "all_exhausted"),  # opencode exhausted
            ({"api_key": "gk2", "key_hash": "ghash2"}, "gemini-3.5-flash", None),
        ]
        response_effects = [("Vision result", 10)]

        use_case = _make_use_case(resolve_effects, response_effects)

        mock_settings = MagicMock()
        mock_settings.DEFAULT_MODEL = "gemini-3.5-flash"
        mock_settings.RESEARCH_MODEL = "gemini-3.5-flash"
        mock_settings.QNA_MODEL = "gemini-3.1-flash-lite"
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

        captured_fallback_model: list[str] = []

        original_get_response = ProviderRouter.get_response

        async def _spy_get_response(self, preferred_model, *args, **kwargs):
            captured_fallback_model.append(preferred_model)
            return await original_get_response(self, preferred_model, *args, **kwargs)

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
            patch.object(ProviderRouter, "get_response", _spy_get_response),
        ):
            router = ProviderRouter()
            await router.get_response(
                "opencode-go/mimo-v2-omni",
                history,
                max_key_retries=1,
            )

        # Second call must be the Gemini vision fallback model
        assert len(captured_fallback_model) >= 2
        assert captured_fallback_model[1] == "gemini-3.5-flash"

    @pytest.mark.asyncio
    async def test_is_fallback_flag_prevents_infinite_recursion(self):
        """When _is_fallback=True, exhaustion must NOT trigger a second cascade."""
        fake_status = FakeKeyStatusManager()

        resolve_effects = [
            (None, None, "all_exhausted"),
        ]
        use_case = _make_use_case(resolve_effects, [])

        mock_settings = MagicMock()
        mock_settings.DEFAULT_MODEL = "gemini-3.5-flash"
        mock_settings.RESEARCH_MODEL = "gemini-3.5-flash"
        mock_settings.QNA_MODEL = "gemini-3.1-flash-lite"
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash"]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            router = ProviderRouter()
            text, tokens = await router.get_response(
                "opencode-go/minimax-m2.7",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=1,
                _is_fallback=True,  # Already in fallback — must not recurse
            )

        # Should get an error message, not a response
        assert "🚫" in text
        assert tokens is None
        # resolve_ai_request should only have been called once
        assert use_case.resolve_ai_request.call_count == 1

    @pytest.mark.asyncio
    async def test_non_opencode_model_bypasses_cascade(self):
        """A plain Gemini model must NOT trigger the Opencode cascade pathway."""
        fake_status = FakeKeyStatusManager()

        resolve_effects = [
            (None, None, "all_exhausted"),
        ]
        use_case = _make_use_case(resolve_effects, [])

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            router = ProviderRouter()
            text, tokens = await router.get_response(
                "gemini-3.5-flash",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=1,
            )

        # Error must mention "Gemini" — not Opencode
        assert "Gemini" in text or "🚫" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_gemini_fallback_exhausted_surfaces_error(self):
        """If Gemini fallback is also exhausted, a user-facing error is returned."""
        fake_status = FakeKeyStatusManager()

        resolve_effects = [
            (None, None, "all_exhausted"),  # Opencode exhausted
            (None, None, "all_exhausted"),  # Gemini fallback also exhausted
        ]
        use_case = _make_use_case(resolve_effects, [])

        mock_settings = MagicMock()
        mock_settings.DEFAULT_MODEL = "gemini-3.5-flash"
        mock_settings.RESEARCH_MODEL = "gemini-3.5-flash"
        mock_settings.QNA_MODEL = "gemini-3.1-flash-lite"
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            router = ProviderRouter()
            text, tokens = await router.get_response(
                "opencode-go/minimax-m2.7",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=1,
            )

        assert "🚫" in text
        assert tokens is None


# ── OC-02: typed Opencode stream cascade ─────────────────────────────────────


class TestOpencodeStreamCascade:
    @pytest.mark.asyncio
    async def test_opencode_stream_exhaustion_cascades_to_gemini(self):
        """Typed stream routes exhausted Opencode directly to Gemini."""
        from app.providers.stream_types import (
            FinishReason,
            GenerationRequest,
            GroundingReport,
            PromptRole,
            PromptTurn,
            ProviderKind,
            RouteUsed,
            StreamCompleted,
            TextDelta,
            TextPart,
            TokenUsage,
        )

        fake_status = FakeKeyStatusManager()
        use_case = _make_use_case(
            [
                (None, None, "all_exhausted"),
                ({"api_key": "gemini-key", "key_hash": "gemini-hash"}, "gemini-3.5-flash", None),
                (None, None, "all_exhausted"),
            ],
            [],
        )
        mock_settings = MagicMock(
            DEFAULT_MODEL="gemini-3.5-flash",
            RESEARCH_MODEL="gemini-3.5-flash",
            QNA_MODEL="gemini-3.1-flash-lite",
            AVAILABLE_MODELS=["gemini-3.5-flash", "gemini-3.1-flash-lite"],
        )

        class GeminiProvider:
            provider_name = "gemini"

            async def stream(self, request, *, model_name):
                yield TextDelta("chunk from gemini")
                yield StreamCompleted(
                    finish_reason=FinishReason.from_raw("STOP"),
                    usage=TokenUsage(total=4),
                    grounding=GroundingReport(),
                    route=RouteUsed(
                        provider=ProviderKind.GEMINI,
                        requested_model=request.models[0],
                        actual_model=model_name,
                    ),
                )

        request = GenerationRequest(
            models=("opencode-go/kimi-k2.5",),
            turns=(PromptTurn(PromptRole.USER, (TextPart("stream test"),)),),
            allow_deferred=False,
        )
        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
            patch("app.providers.base.get_provider_for_model", return_value=GeminiProvider()),
        ):
            events = [event async for event in ProviderRouter().stream(request)]

        assert events[0] == TextDelta("chunk from gemini")
        assert isinstance(events[1], StreamCompleted)
        assert events[1].route.actual_model == "gemini-3.5-flash"
        assert "gemini-hash" in fake_status.successes
