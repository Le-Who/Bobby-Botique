"""
Tests for ProviderRouter and KeyStatusManager.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.errors import ErrorCode
from app.providers import ProviderRouter
from app.providers.router import _ordered_gemini_fallback_models
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    ProviderKind,
    ProviderStreamProtocolError,
    RequestScope,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    TextPart,
    TokenUsage,
    Workload,
)

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeKeyStatusManager:
    """In-memory KeyStatusManager that tracks suspended keys and recorded successes."""

    def __init__(self) -> None:
        self.suspended_keys: dict[str, dict] = {}  # key_hash → {'category': ..., 'error_text': ...}
        self.successful_keys: list[str] = []

    async def suspend_key(
        self,
        key_hash: str,
        model_name: str,
        error_category: str,
        error_text: str = "",
    ) -> None:
        self.suspended_keys[key_hash] = {
            "category": error_category,
            "error_text": error_text,
        }

    async def record_success(self, key_hash: str, model_name: str) -> None:
        if key_hash not in self.suspended_keys:
            self.successful_keys.append(key_hash)


class FakeAgentRequestUseCase:
    """Fake use case to control the responses for resolve/get/increment."""

    def __init__(self, resolve_sequence: list, response_sequence: list) -> None:
        self.resolve_sequence = list(resolve_sequence)
        self.response_sequence = list(response_sequence)
        self.resolves_made = 0
        self.responses_made = 0
        self.resolve_calls: list[dict] = []
        self.response_calls: list[dict] = []
        self.usages_incremented: list[str] = []

    async def resolve_ai_request(
        self, model_name: str, use_openrouter: bool = False, excluded_key_hashes: set[str] | None = None, **kwargs
    ) -> tuple[dict | None, str | None, str | None]:
        self.resolves_made += 1
        self.resolve_calls.append(
            {
                "preferred_model": model_name,
                "use_openrouter": use_openrouter,
                "excluded_key_hashes": excluded_key_hashes,
                **kwargs,
            }
        )
        if self.resolve_sequence:
            return self.resolve_sequence.pop(0)
        return (None, None, "all_exhausted")

    async def get_ai_response(self, *args, **kwargs) -> tuple[str, int | None]:
        self.responses_made += 1
        self.response_calls.append({"args": args, "kwargs": kwargs})
        response = self.response_sequence.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def increment_key_usage(self, key_hash: str, model_name: str, use_openrouter: bool = False) -> None:
        self.usages_incremented.append(key_hash)


def _typed_request(model: str = "gemini-3.5-flash") -> GenerationRequest:
    return GenerationRequest(
        models=(model,),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hi"),)),),
    )


def _completion(model: str, total: int) -> StreamCompleted:
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=total),
        grounding=GroundingReport(),
        route=RouteUsed(
            provider=ProviderKind.GEMINI,
            requested_model=model,
            actual_model=model,
        ),
    )


@pytest.mark.asyncio
async def test_typed_router_single_key_forwards_terminal_metadata():
    router = ProviderRouter()
    status = FakeKeyStatusManager()
    use_case = FakeAgentRequestUseCase(
        resolve_sequence=[
            ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
            (None, None, "all_exhausted"),
        ],
        response_sequence=[],
    )

    class Provider:
        async def stream(self, request, *, model_name):
            yield TextDelta("Hello")
            yield _completion(model_name, 17)

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        events = [event async for event in router.stream(_typed_request())]

    assert events == [TextDelta("Hello"), _completion("gemini-3.5-flash", 17)]
    assert status.successful_keys == ["hash1"]
    assert use_case.usages_incremented == ["hash1"]


@pytest.mark.asyncio
async def test_typed_router_rejects_single_provider_event_after_terminal():
    router = ProviderRouter()
    use_case = FakeAgentRequestUseCase(
        resolve_sequence=[
            ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
            (None, None, "all_exhausted"),
        ],
        response_sequence=[],
    )

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            yield TextDelta("Hello")
            yield _completion(model_name, 17)
            yield TextDelta("late")

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
        patch("app.repos.keys.get_key_status_manager", return_value=FakeKeyStatusManager()),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
    ):
        with pytest.raises(ProviderStreamProtocolError, match="after terminal"):
            _ = [event async for event in router.stream(_typed_request())]


@pytest.mark.asyncio
async def test_typed_router_race_keeps_winner_metadata_and_awaits_loser():
    router = ProviderRouter()
    status = FakeKeyStatusManager()
    use_case = FakeAgentRequestUseCase(
        resolve_sequence=[
            ({"api_key": "winner", "key_hash": "hash1"}, "gemini-3.5-flash", None),
            ({"api_key": "loser", "key_hash": "hash2"}, "gemini-3.5-flash", None),
        ],
        response_sequence=[],
    )
    loser_closed = False

    class Provider:
        def __init__(self, key: str):
            self.key = key

        async def stream(self, request, *, model_name):
            nonlocal loser_closed
            if self.key == "winner":
                yield TextDelta("Winner")
                yield _completion(model_name, 23)
                return
            try:
                await asyncio.sleep(60)
                yield TextDelta("Loser")
            finally:
                loser_closed = True

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch(
            "app.providers.base.get_provider_for_model",
            side_effect=lambda _model, key: Provider(key),
        ),
    ):
        events = [event async for event in router.stream(_typed_request())]

    assert events == [TextDelta("Winner"), _completion("gemini-3.5-flash", 23)]
    assert loser_closed is True
    assert status.successful_keys == ["hash1"]


@pytest.mark.asyncio
async def test_typed_router_rotates_pre_text_failure_without_leaking_error_text():
    router = ProviderRouter()
    status = FakeKeyStatusManager()
    use_case = FakeAgentRequestUseCase(
        resolve_sequence=[
            ({"api_key": "bad", "key_hash": "hash1"}, "gemini-3.5-flash", None),
            (None, None, "all_exhausted"),
            ({"api_key": "good", "key_hash": "hash2"}, "gemini-3.5-flash", None),
            (None, None, "all_exhausted"),
        ],
        response_sequence=[],
    )

    class Provider:
        def __init__(self, key: str):
            self.key = key

        async def stream(self, request, *, model_name):
            if self.key == "bad":
                yield StreamFailed(
                    code=ErrorCode.RATE_LIMIT,
                    phase=FailurePhase.BEFORE_TEXT,
                    retry=RetryDisposition.TRY_NEXT_KEY,
                    key=KeyDisposition.RATE_LIMITED,
                    diagnostic="HTTP 429",
                )
                return
            yield TextDelta("Recovered")
            yield _completion(model_name, 9)

    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch(
            "app.providers.base.get_provider_for_model",
            side_effect=lambda _model, key: Provider(key),
        ),
    ):
        events = [event async for event in router.stream(_typed_request())]

    assert events == [TextDelta("Recovered"), _completion("gemini-3.5-flash", 9)]
    assert status.suspended_keys["hash1"]["category"] == "rate_limit"


@pytest.mark.asyncio
async def test_typed_router_returns_deferred_terminal_instead_of_status_text():
    router = ProviderRouter()
    status = FakeKeyStatusManager()
    use_case = FakeAgentRequestUseCase(
        resolve_sequence=[
            ({"api_key": "bad", "key_hash": "hash1"}, "gemini-3.5-flash", None),
            (None, None, "all_exhausted"),
            (None, None, "all_exhausted"),
        ],
        response_sequence=[],
    )

    class Provider:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            yield StreamFailed(
                code=ErrorCode.OVERLOADED,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="HTTP 503",
            )

    request = GenerationRequest(
        models=("gemini-3.5-flash",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hi"),)),),
        scope=RequestScope(user_id=7, chat_id=-100),
    )
    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
        patch.object(router, "_pick_transient_fallback_model", return_value=None),
        patch(
            "app.deferred_response.enqueue_deferred_generation",
            new_callable=AsyncMock,
            return_value="task-42",
        ),
    ):
        events = [event async for event in router.stream(request)]

    assert events == [StreamDeferred("task-42")]


@pytest.mark.asyncio
async def test_typed_inline_router_preserves_four_key_attempt_rounds():
    router = ProviderRouter()
    status = FakeKeyStatusManager()
    resolve_sequence = []
    for index in range(4):
        resolve_sequence.extend(
            [
                (
                    {"api_key": f"bad-{index}", "key_hash": f"hash-{index}"},
                    "vendor/model",
                    None,
                ),
                (None, None, "all_exhausted"),
            ]
        )
    use_case = FakeAgentRequestUseCase(resolve_sequence, response_sequence=[])

    class Provider:
        provider_name = "openrouter"

        async def stream(self, request, *, model_name):
            yield StreamFailed(
                code=ErrorCode.INVALID_KEY,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.INVALID,
                diagnostic="invalid key",
            )

    request = GenerationRequest(
        models=("vendor/model",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hi"),)),),
        workload=Workload.INLINE,
        allow_deferred=False,
    )
    with (
        patch("app.agent_use_cases.AgentRequestUseCase", return_value=use_case),
        patch("app.repos.keys.get_key_status_manager", return_value=status),
        patch("app.providers.base.get_provider_for_model", return_value=Provider()),
        patch("app.providers.router._ordered_gemini_fallback_models", return_value=[]),
        patch.object(router, "_pick_transient_fallback_model", return_value=None),
    ):
        events = [event async for event in router.stream(request)]

    assert len(status.suspended_keys) == 4
    assert isinstance(events[-1], StreamFailed)


def test_dynamic_configured_gemini_model_remains_eligible_for_fallback():
    """Future Gemini IDs from config must not be discarded by a static allowlist."""
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]
    mock_settings.DEFAULT_MODEL = "gemini-3.7-flash"
    mock_settings.RESEARCH_MODEL = "gemini-3.7-flash"
    mock_settings.QNA_MODEL = "gemini-3.5-flash-lite"
    mock_settings.INLINE_MODEL = "gemini-3.5-flash-lite"

    with patch("app.providers.router.settings", mock_settings):
        fallbacks = _ordered_gemini_fallback_models("gemini-3.6-flash")

    assert "gemini-3.7-flash" in fallbacks


def test_custom_gemini_model_gets_full_known_router_fallback_chain():
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-3.7-flash"]
    mock_settings.DEFAULT_MODEL = "gemini-3.7-flash"
    mock_settings.RESEARCH_MODEL = "gemini-3.7-flash"
    mock_settings.QNA_MODEL = "gemini-3.7-flash"
    mock_settings.INLINE_MODEL = "gemini-3.7-flash"

    with patch("app.providers.router.settings", mock_settings):
        fallbacks = _ordered_gemini_fallback_models("gemini-3.7-flash")

    assert fallbacks[:4] == [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


def test_transient_fallback_uses_next_known_model_even_when_hidden():
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-3.7-flash"]
    mock_settings.DEFAULT_MODEL = "gemini-3.7-flash"
    mock_settings.RESEARCH_MODEL = "gemini-3.7-flash"
    mock_settings.QNA_MODEL = "gemini-3.7-flash"
    mock_settings.INLINE_MODEL = "gemini-3.7-flash"
    router = ProviderRouter()

    with patch("app.providers.router.settings", mock_settings):
        custom_fallback = router._pick_transient_fallback_model("gemini-3.7-flash", False)
        primary_fallback = router._pick_transient_fallback_model("gemini-3.6-flash", False)

    assert custom_fallback == "gemini-3.6-flash"
    assert primary_fallback == "gemini-3.5-flash"


# ── Tests for ProviderRouter.get_response ──────────────────────────────────────


class TestProviderRouter:
    @pytest.mark.asyncio
    async def test_successful_response(self, caplog):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "AIzaTEST-key-1", "key_hash": "hash1"}, "gemini-3.1", None),
            ],
            response_sequence=[
                ("Hello!", 10),
            ],
        )

        with (
            caplog.at_level(logging.INFO),
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}])

        assert text == "Hello!"
        assert tokens == 10
        assert "hash1" in fake_status.successful_keys
        assert fake_use_case.usages_incremented == ["hash1"]
        assert "KEY_EVENT key_request key=AIzaTEST" in caplog.text
        assert "KEY_EVENT key_answered key=AIzaTEST" in caplog.text
        assert "model=gemini-3.1" in caplog.text
        assert "provider=gemini" in caplog.text
        assert "tokens=10" in caplog.text
        assert caplog.text.index("KEY_EVENT key_request") < caplog.text.index("KEY_EVENT key_answered")

    @pytest.mark.asyncio
    async def test_all_keys_exhausted(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                (None, None, "all_exhausted"),
            ],
            response_sequence=[],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}])

        assert "🚫" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_key_failure_triggers_retry_and_suspend(self):
        """Permanent key error should suspend key and trigger retry."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.1", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("Hello!", 10),  # success
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert text == "Hello!"
        assert tokens == 10
        assert "hash1" in fake_status.suspended_keys
        assert fake_status.suspended_keys["hash1"]["category"] == "permanent"
        assert "hash2" not in fake_status.suspended_keys
        assert "hash2" in fake_status.successful_keys

    @pytest.mark.asyncio
    async def test_timeout_exception_triggers_retry_with_different_key(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.1", None),
            ],
            response_sequence=[
                TimeoutError(),
                ("Recovered response", 12),
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=2,
                timeout=0.1,
            )

        assert text == "Recovered response"
        assert tokens == 12
        assert fake_use_case.resolve_calls[1]["excluded_key_hashes"] == {"hash1"}
        assert fake_status.suspended_keys["hash1"]["category"] == "transient"
        assert "hash2" in fake_status.successful_keys

    @pytest.mark.asyncio
    async def test_router_disables_provider_retries_so_same_model_rotates_keys(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.5-flash", None),
            ],
            response_sequence=[
                TimeoutError(),
                ("Recovered response", 12),
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response(
                "gemini-3.5-flash",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=2,
            )

        assert text == "Recovered response"
        assert tokens == 12
        assert fake_use_case.response_calls[0]["kwargs"]["provider_max_retries"] == 1
        assert fake_use_case.response_calls[1]["kwargs"]["provider_max_retries"] == 1
        assert fake_use_case.resolve_calls[1]["excluded_key_hashes"] == {"hash1"}

    @pytest.mark.asyncio
    async def test_transient_failures_cascade_to_lite_for_non_stream_response(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.5-flash", None),
                ({"api_key": "k3", "key_hash": "hash3"}, "gemini-3.5-flash-lite", None),
            ],
            response_sequence=[
                TimeoutError(),
                ("⏰ Превышено время ожидания ответа от API.", None),
                ("Lite fallback response", 9),
            ],
        )

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            text, tokens = await router.get_response(
                "gemini-3.5-flash",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=2,
                timeout=0.1,
            )

        assert text == "Lite fallback response"
        assert tokens == 9
        assert fake_use_case.resolve_calls[2]["preferred_model"] == "gemini-3.5-flash-lite"

    @pytest.mark.asyncio
    async def test_transient_grounding_failures_cascade_to_2_5_lite_for_non_stream_response(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-2.5-flash", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-2.5-flash-lite", None),
            ],
            response_sequence=[
                TimeoutError(),
                ("Grounded lite fallback response", 11),
            ],
        )

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            text, tokens = await router.get_response(
                "gemini-2.5-flash",
                [{"role": "user", "parts": ["курс доллара сегодня"]}],
                max_key_retries=1,
                timeout=0.1,
            )

        assert text == "Grounded lite fallback response"
        assert tokens == 11
        assert fake_use_case.resolve_calls[1]["preferred_model"] == "gemini-2.5-flash-lite"

    @pytest.mark.asyncio
    async def test_quota_error_suspends_with_quota_category(self):
        """Quota exceeded should suspend with 'quota' category."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                (None, None, "all_exhausted"),
            ],
            response_sequence=[
                ("🚫 Достигнут лимит запросов к API (Quota Exceeded).", None),
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}])

        assert "hash1" in fake_status.suspended_keys
        assert fake_status.suspended_keys["hash1"]["category"] == "quota"

    @pytest.mark.asyncio
    async def test_openrouter_detection(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                (None, None, "all_exhausted"),
            ],
            response_sequence=[],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, _ = await router.get_response("openai/gpt-4o", [{"role": "user", "parts": ["hi"]}])

        assert "OpenRouter" in text

    @pytest.mark.asyncio
    async def test_transient_error_is_suspended_temporarily(self):
        """503/timeout errors should suspend the key briefly and retry another key."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                (None, None, "all_exhausted"),
            ],
            response_sequence=[
                ("⏰ Превышено время ожидания ответа от API.", None),  # transient, from timeout
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}], max_key_retries=3)

        assert "hash1" in fake_status.suspended_keys
        assert fake_status.suspended_keys["hash1"]["category"] == "transient"

    @pytest.mark.asyncio
    async def test_model_fallback_on_consecutive_permanent_errors(self):
        """If a given model has too many permanent errors in a row, the router falls back to the next model."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.5-flash", None),
                ({"api_key": "k3", "key_hash": "hash3"}, "gemini-3.5-flash", None),
                ({"api_key": "k4", "key_hash": "hash4"}, "gemini-3.1-flash-lite", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🔑 Неверный API ключ.", None),  # permanent
                ("Fallback response!", 15),  # success on fallback model
            ],
        )

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = [
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
        ]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            text, tokens = await router.get_response(
                "gemini-3.5-flash", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert text == "Fallback response!"
        assert tokens == 15

        # 3 keys from the first model should be suspended
        assert "hash1" in fake_status.suspended_keys
        assert "hash2" in fake_status.suspended_keys
        assert "hash3" in fake_status.suspended_keys

        assert "hash4" in fake_status.successful_keys

    @pytest.mark.asyncio
    async def test_model_fallback_uses_3_5_lite_after_3_5_flash_failures(self):
        """Permanent failure of all 3.5 keys should follow the known chain to 3.5 Flash Lite."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.5-flash", None),
                ({"api_key": "k3", "key_hash": "hash3"}, "gemini-3.5-flash", None),
                ({"api_key": "k4", "key_hash": "hash4"}, "gemini-3.5-flash-lite", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),
                ("🔑 Неверный API ключ.", None),
                ("🔑 Неверный API ключ.", None),
                ("Lite fallback response!", 10),
            ],
        )

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = [
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
        ]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            text, tokens = await router.get_response(
                "gemini-3.5-flash", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert text == "Lite fallback response!"
        assert tokens == 10
        assert fake_use_case.resolve_calls[3]["preferred_model"] == "gemini-3.5-flash-lite"

    @pytest.mark.asyncio
    async def test_no_model_fallback_on_non_permanent_errors(self, monkeypatch):
        """Model fallback should NOT trigger for quota/rate-limit errors."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.5-flash", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.5-flash", None),
                ({"api_key": "k3", "key_hash": "hash3"}, "gemini-3.5-flash", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🚫 Достигнут лимит запросов к API (Quota Exceeded).", None),  # quota (not permanent to model)
                ("🔑 Неверный API ключ.", None),  # permanent
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response(
                "gemini-3.5-flash", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert "🚫" in text
        assert tokens is None
