"""
Tests for the AI provider abstraction layer.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.circuit_breaker import CircuitBreaker
from app.errors import ErrorCode, extract_error_code
from app.providers import (
    AIResponse,
    BaseAIProvider,
    GeminiProvider,
    OpenRouterProvider,
    get_ai_response,
    is_openrouter_model,
)
from app.providers.gemini import _gemini_clients_cache
from app.utils.api_logger import APILogger


@pytest.fixture
async def stalled_gemini_attempt(monkeypatch):
    started = asyncio.Event()
    drained = asyncio.Event()

    async def stalled_call(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=stalled_call)))
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda key: client)
    monkeypatch.setattr("app.providers.gemini.api_logger", APILogger())
    monkeypatch.setattr("app.providers.gemini.metrics_collector", AsyncMock())
    breaker = CircuitBreaker("gemini-timeout-test")
    monkeypatch.setattr("app.resilience_policy.get_circuit_breaker", lambda name: breaker)
    try:
        yield SimpleNamespace(provider=GeminiProvider("dummy-provider-key-CANC"), started=started, drained=drained)
    finally:
        await breaker.shutdown()


@pytest.mark.parametrize("cause", ["outer_timeout", "caller_cancellation"])
async def test_gemini_cancelled_sdk_attempt_has_one_honest_terminal(stalled_gemini_attempt, caplog, cause):
    attempt = stalled_gemini_attempt
    with caplog.at_level(logging.INFO, logger="api_logger"):
        task = asyncio.create_task(
            attempt.provider.get_response(
                history=[{"role": "user", "parts": ["synthetic request"]}],
                model_name="gemini-3.5-flash",
                max_retries=1,
                timeout=0.05 if cause == "outer_timeout" else 10.0,
            )
        )
        try:
            await asyncio.wait_for(attempt.started.wait(), timeout=1.0)
            if cause == "caller_cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await task
                assert response.success is False
                assert extract_error_code(response.text) == ErrorCode.TIMEOUT
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert attempt.drained.is_set()
    events = [record for record in caplog.records if record.name == "api_logger"]
    assert [record._event_name for record in events] == ["api.request_started", "api.request_finished"]
    started, terminal = events
    assert terminal.outcome == "cancelled"
    assert terminal.levelno == logging.INFO
    assert terminal.attempt_id == started.attempt_id
    assert terminal.key_suffix == started.key_suffix == "CANC"
    assert terminal.key_fingerprint == started.key_fingerprint
    assert terminal.duration_ms >= 0


async def test_gemini_sdk_timeout_keeps_one_failed_terminal(stalled_gemini_attempt, monkeypatch, caplog):
    async def sdk_timeout(**kwargs):
        raise TimeoutError()

    monkeypatch.setattr(stalled_gemini_attempt.provider._client.aio.models, "generate_content", sdk_timeout)
    with caplog.at_level(logging.INFO, logger="api_logger"):
        response = await stalled_gemini_attempt.provider.get_response(
            history=[{"role": "user", "parts": ["synthetic request"]}],
            model_name="gemini-3.5-flash",
            max_retries=1,
            timeout=10.0,
        )

    assert response.success is False
    assert extract_error_code(response.text) == ErrorCode.TIMEOUT
    events = [record for record in caplog.records if record.name == "api_logger"]
    terminals = [record for record in events if record._event_name == "api.request_finished"]
    assert len(terminals) == 1
    assert terminals[0].outcome == "failed"
    assert terminals[0].attempt_id == events[0].attempt_id


async def test_gemini_cancellation_before_request_does_not_fabricate_terminal(
    stalled_gemini_attempt, monkeypatch, caplog
):
    preparing = asyncio.Event()

    async def stalled_preparation(*args):
        preparing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.providers.gemini.metrics_collector.record_api_call", stalled_preparation)
    with caplog.at_level(logging.INFO, logger="api_logger"):
        task = asyncio.create_task(
            stalled_gemini_attempt.provider.get_response(
                history=[{"role": "user", "parts": ["synthetic request"]}],
                model_name="gemini-3.5-flash",
                max_retries=1,
                timeout=10.0,
            )
        )
        try:
            await asyncio.wait_for(preparing.wait(), timeout=1.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert not stalled_gemini_attempt.started.is_set()
    assert not [record for record in caplog.records if record.name == "api_logger"]


class TestAIResponse:
    """Tests for AIResponse dataclass."""

    def test_successful_response(self):
        response = AIResponse(
            text="Hello, world!",
            token_count=10,
            success=True,
            provider="gemini",
            model="gemini-3.1-flash-lite",
        )
        assert response.text == "Hello, world!"
        assert response.token_count == 10
        assert response.success is True
        assert response.is_error is False
        assert response.error_message is None

    def test_error_response(self):
        response = AIResponse(
            text="❌ Error occurred",
            token_count=0,
            success=False,
            error_message="Something went wrong",
            provider="openrouter",
            model="openai/gpt-4o",
        )
        assert response.success is False
        assert response.is_error is True
        assert response.error_message == "Something went wrong"

    def test_is_error_with_error_message(self):
        """is_error should be True if error_message is set, even if success=True"""
        response = AIResponse(text="text", token_count=5, success=True, error_message="warning")
        assert response.is_error is True


class TestIsOpenRouterModel:
    """Tests for is_openrouter_model function."""

    def test_openrouter_model(self):
        """OpenRouter models contain a slash."""
        assert is_openrouter_model("openai/gpt-4o") is True
        assert is_openrouter_model("anthropic/claude-3-opus") is True
        assert is_openrouter_model("meta-llama/llama-3-70b") is True

    def test_gemini_model(self):
        """Gemini models don't contain a slash."""
        assert is_openrouter_model("gemini-3.1-flash-lite") is False
        assert is_openrouter_model("gemini-3-flash-preview") is False
        assert is_openrouter_model("gemini-pro-vision") is False


class TestBaseAIProvider:
    """Tests for BaseAIProvider abstract class."""

    def test_init_with_valid_key(self):
        """Should initialize with valid API key."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

            async def stream(self, **kwargs):
                yield ""

            def _log_failure(self, start_time, model, msg, user_id, chat_id):
                pass

        provider = TestProvider("valid-key")
        assert provider.api_key == "valid-key"

    def test_init_with_invalid_key(self):
        """Should raise ValueError for invalid API key."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

            async def stream(self, **kwargs):
                yield ""

            def _log_failure(self, start_time, model, msg, user_id, chat_id):
                pass

        with pytest.raises(ValueError, match="api_key must be a non-empty string"):
            TestProvider("")

        with pytest.raises(ValueError, match="api_key must be a non-empty string"):
            TestProvider("   ")

    def test_validate_inputs_empty_history(self):
        """Should return error string for empty history (no longer raises)."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

            async def stream(self, **kwargs):
                yield ""

            def _log_failure(self, start_time, model, msg, user_id, chat_id):
                pass

        provider = TestProvider("key")
        result = provider._validate_inputs([], "model", None, None)
        assert result == "history must be a non-empty list"

    def test_validate_inputs_invalid_model(self):
        """Should return error string for invalid model name."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

            async def stream(self, **kwargs):
                yield ""

            def _log_failure(self, start_time, model, msg, user_id, chat_id):
                pass

        provider = TestProvider("key")
        result = provider._validate_inputs([{"role": "user", "parts": ["hi"]}], "", None, None)
        assert result == "model_name must be a non-empty string"

    def test_is_transient_error(self):
        """Should correctly identify transient errors."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

            async def stream(self, **kwargs):
                yield ""

            def _log_failure(self, start_time, model, msg, user_id, chat_id):
                pass

        provider = TestProvider("key")

        # Transient errors
        assert provider._is_transient_error("503 Service Unavailable") is True
        assert provider._is_transient_error("Server overloaded") is True
        assert provider._is_transient_error("Rate limit exceeded") is True
        assert provider._is_transient_error("Connection timeout") is True

        # Non-transient errors
        assert provider._is_transient_error("Invalid API key") is False
        assert provider._is_transient_error("Quota exceeded") is False


class TestProviders:
    """Tests for provider classes."""

    @pytest.mark.asyncio
    async def test_gemini_wrapper_success(self):
        """GeminiProvider should return AIResponse on success."""
        _gemini_clients_cache.clear()

        mock_response = MagicMock()
        mock_response.text = "Hello!"
        # New: token count comes from usage_metadata on the response
        mock_usage = MagicMock()
        mock_usage.total_token_count = 15
        mock_response.usage_metadata = mock_usage

        with (
            patch("app.providers.gemini.genai.Client") as MockClient,
            patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
            patch("app.providers.gemini.api_logger", new_callable=MagicMock) as mock_api_logger,
            patch("app.providers.gemini.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []

            wrapper = GeminiProvider("AIza123456789")

            mock_aio = MagicMock()
            mock_aio.generate_content = AsyncMock(return_value=mock_response)
            MockClient.return_value.aio.models = mock_aio

            response = await wrapper._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-3.1-flash-lite",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=120.0,
            )

            assert response.text == "Hello!"
            assert response.token_count == 15
            assert response.success is True
            assert response.provider == "gemini"
            assert mock_api_logger.log_request.call_args.kwargs["api_key"] == "AIza123456789"
            assert "key_prefix" not in mock_api_logger.log_request.call_args.kwargs
            assert "key_prefix" not in mock_api_logger.log_response.call_args.kwargs

    @pytest.mark.asyncio
    async def test_gemini_wrapper_error(self):
        """GeminiProvider should detect error responses."""
        _gemini_clients_cache.clear()

        mock_response = MagicMock()
        mock_response.text = None  # Empty response triggers error

        with (
            patch("app.providers.gemini.genai.Client") as MockClient,
            patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
            patch("app.providers.gemini.api_logger", new_callable=MagicMock),
            patch("app.providers.gemini.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []

            wrapper = GeminiProvider("test-key")

            mock_aio = MagicMock()
            mock_aio.generate_content = AsyncMock(return_value=mock_response)
            MockClient.return_value.aio.models = mock_aio

            response = await wrapper._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-3.1-flash-lite",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=120.0,
            )

            assert response.success is False
            assert response.is_error is True

    @pytest.mark.asyncio
    async def test_gemini_wrapper_deadline_exceeded(self):
        """GeminiProvider should retry on 504 DEADLINE_EXCEEDED."""
        from google.genai.errors import APIError

        _gemini_clients_cache.clear()

        # We will mock _execute_request to raise APIError for testing the raise logic,
        # but wait, the raise logic is INSIDE _execute_request!
        # So we mock the underlying genai Client to raise APIError("504 DEADLINE_EXCEEDED").
        with (
            patch("app.providers.gemini.genai.Client") as MockClient,
            patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
            patch("app.providers.gemini.api_logger", new_callable=MagicMock),
            patch("app.providers.gemini.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []
            wrapper = GeminiProvider("test-key")

            class FakeAPIError(APIError):
                def __init__(self, msg):
                    self.msg = msg

                def __str__(self):
                    return self.msg

            mock_aio = MagicMock()
            # Raise the error
            mock_aio.generate_content = AsyncMock(side_effect=FakeAPIError("504 DEADLINE_EXCEEDED"))
            MockClient.return_value.aio.models = mock_aio

            # BaseAIProvider wraps _execute_request in run_with_resilience.
            # If it is retryable, it will be called `max_retries` times (if max_retries=2, called 2 times).
            # Let's override the resilience policy to speed it up.
            response = await wrapper.get_response(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-3.1-flash-lite",
                max_retries=2,
            )

            # _execute_request should have been called 2 times (1 initial + 1 retry)
            assert mock_aio.generate_content.call_count == 2
            assert response.success is False

    @pytest.mark.asyncio
    async def test_openrouter_wrapper_success(self):
        """OpenRouterProvider should return AIResponse on success."""
        wrapper = OpenRouterProvider("test-key")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Greetings!"}}],
            "usage": {"total_tokens": 20},
        }
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("app.providers.openrouter._openrouter_http_client") as mock_client,
            patch("app.providers.openrouter.metrics_collector", new_callable=AsyncMock),
            patch("app.providers.openrouter.api_logger", new_callable=MagicMock),
        ):
            mock_client.post = AsyncMock(return_value=mock_resp)

            response = await wrapper._execute_request(
                history=[{"role": "user", "parts": ["hello"]}],
                model_name="openai/gpt-4o",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=120.0,
            )

            assert response.text == "Greetings!"
            assert response.token_count == 20
            assert response.success is True
            assert response.provider == "openrouter"

    @pytest.mark.asyncio
    async def test_openrouter_typed_stream_preserves_usage_and_finish_reason(self):
        from app.providers.stream_types import (
            FinishKind,
            GenerationRequest,
            PromptRole,
            PromptTurn,
            StreamCompleted,
            TextDelta,
            TextPart,
            Workload,
        )

        class FakeStreamResponse:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}'
                yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}'
                yield "data: [DONE]"

        request = GenerationRequest(
            models=("openai/gpt-4o",),
            turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
            workload=Workload.INLINE,
        )
        provider = OpenRouterProvider("test-key")
        client = MagicMock()
        client.stream.return_value = FakeStreamResponse()

        with patch("app.providers.openrouter._openrouter_http_client", client):
            events = [event async for event in provider.stream(request, model_name="openai/gpt-4o")]

        assert events[0] == TextDelta("Hi")
        assert isinstance(events[1], StreamCompleted)
        assert events[1].finish_reason.kind is FinishKind.STOP
        assert events[1].usage.prompt == 3
        assert events[1].usage.completion == 2
        assert events[1].usage.total == 5
        assert client.stream.call_args.kwargs["timeout"] == 45.0

    @pytest.mark.asyncio
    async def test_openrouter_empty_local_payload_has_validation_terminal_before_http(self):
        from app.providers.stream_types import (
            GenerationRequest,
            PromptRole,
            PromptTurn,
            StreamFailed,
            TextPart,
        )

        request = GenerationRequest(
            models=("openai/gpt-4o",),
            turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        )
        provider = OpenRouterProvider("test-key-9876")
        client = MagicMock()

        with (
            patch("app.providers.openrouter.openai_messages", new_callable=AsyncMock, return_value=[]),
            patch("app.providers.openrouter._openrouter_http_client", client),
            patch("app.providers.openrouter.record_provider_validation_failure") as validation_terminal,
        ):
            events = [event async for event in provider.stream(request, model_name="openai/gpt-4o")]

        assert isinstance(events[0], StreamFailed)
        validation_terminal.assert_called_once_with(
            provider="openrouter",
            requested_model="openai/gpt-4o",
            actual_model="openai/gpt-4o",
            api_key="test-key-9876",
            validation_code="empty_messages",
        )
        client.stream.assert_not_called()


class TestGetAIResponse:
    """Tests for unified get_ai_response function."""

    @pytest.mark.asyncio
    async def test_delegates_to_gemini(self):
        """Should use Gemini for non-slash models."""
        with patch("app.providers.base.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(
                return_value=AIResponse(text="Gemini response", token_count=10, success=True)
            )
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-3.1-flash-lite",
            )

            assert text == "Gemini response"
            assert tokens == 10

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        """Should return None for token_count on error."""
        with patch("app.providers.base.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(
                return_value=AIResponse(text="❌ Error", token_count=0, success=False)
            )
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="openai/gpt-4o",
            )

            assert text == "❌ Error"
            assert tokens is None
