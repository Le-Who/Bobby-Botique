"""
Tests for GeminiProvider._execute_request via the Provider class directly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai.errors import APIError

from app.providers.gemini import GeminiProvider, _gemini_clients_cache


@pytest.mark.asyncio
async def test_execute_gemini_request_success():
    """Test GeminiProvider._execute_request happy path."""
    _gemini_clients_cache.clear()

    with (
        patch("app.providers.gemini.genai.Client") as MockClient,
        patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
        patch("app.providers.gemini.api_logger", new_callable=MagicMock),
        patch("app.providers.gemini.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        provider = GeminiProvider("key")

        mock_client_instance = MockClient.return_value
        mock_response = MagicMock()
        mock_response.text = "Generated Text"
        # Token count from usage_metadata (not separate count_tokens call)
        mock_usage = MagicMock()
        mock_usage.total_token_count = 50
        mock_response.usage_metadata = mock_usage

        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(return_value=mock_response)
        mock_client_instance.aio.models = mock_aio_models

        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="model",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=100.0,
        )

        assert resp.text == "Generated Text"
        assert resp.token_count == 50
        assert resp.success is True


@pytest.mark.asyncio
async def test_execute_gemini_request_503_error():
    """Test GeminiProvider._execute_request with 503 error raises for retry."""
    _gemini_clients_cache.clear()

    with (
        patch("app.providers.gemini.genai.Client") as MockClient,
        patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
        patch("app.providers.gemini.api_logger", new_callable=MagicMock),
        patch("app.providers.gemini.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        provider = GeminiProvider("key")

        mock_client_instance = MockClient.return_value
        mock_response_obj = MagicMock()
        mock_response_obj.status_code = 503
        mock_response_obj.text = "Service Unavailable"
        error = APIError(503, mock_response_obj)

        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(side_effect=error)
        mock_client_instance.aio.models = mock_aio_models

        with pytest.raises(APIError, match="503"):
            await provider._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="model",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=100.0,
            )


@pytest.mark.asyncio
async def test_execute_gemini_request_other_error():
    """Test GeminiProvider._execute_request with non-retryable error."""
    _gemini_clients_cache.clear()

    with (
        patch("app.providers.gemini.genai.Client") as MockClient,
        patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
        patch("app.providers.gemini.api_logger", new_callable=MagicMock),
        patch("app.providers.gemini.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        provider = GeminiProvider("key")

        mock_client_instance = MockClient.return_value
        mock_response_obj = MagicMock()
        mock_response_obj.status_code = 400
        mock_response_obj.text = "Bad Request"
        error = APIError(400, mock_response_obj)

        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(side_effect=error)
        mock_client_instance.aio.models = mock_aio_models

        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="model",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=100.0,
        )

        assert resp.success is False
        assert "ошибка" in resp.text.lower() or "Bad Request" in resp.text


@pytest.mark.asyncio
async def test_execute_gemini_request_timeout():
    """Test GeminiProvider._execute_request timeout."""
    _gemini_clients_cache.clear()

    def timeout_side_effect(coro, timeout=None):
        coro.close()
        raise TimeoutError("Timeout")

    with (
        patch("app.providers.gemini.genai.Client") as _MockClient,
        patch("app.providers.gemini.metrics_collector", new_callable=AsyncMock),
        patch("app.providers.gemini.api_logger", new_callable=MagicMock),
        patch("app.providers.gemini.settings") as mock_settings,
        patch("asyncio.wait_for", side_effect=timeout_side_effect),
    ):
        mock_settings.SAFETY_SETTINGS = []

        provider = GeminiProvider("key")

        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="model",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=100.0,
        )

        assert resp.success is False
        assert "Превышено время ожидания" in resp.text


@pytest.mark.asyncio
async def test_stream_force_grounding_uses_current_google_search_tool():
    """Current Gemini grounding models use google_search, not google_search_retrieval."""
    _gemini_clients_cache.clear()

    class FakeChunk:
        text = "grounded"
        candidates = []
        usage_metadata = None

    async def fake_stream():
        yield FakeChunk()

    from app.providers.stream_types import (
        GenerationRequest,
        GroundingMode,
        PromptRole,
        PromptTurn,
        TextDelta,
        TextPart,
    )

    request = GenerationRequest(
        models=("gemini-2.5-flash",),
        turns=(
            PromptTurn(
                PromptRole.USER,
                (TextPart("курс доллара сегодня"),),
            ),
        ),
        grounding=GroundingMode.PROVIDER_SEARCH_REQUIRED,
    )

    with (
        patch("app.providers.gemini.genai.Client") as MockClient,
        patch("app.providers.gemini.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []
        provider = GeminiProvider("key")

        mock_client_instance = MockClient.return_value
        mock_aio_models = MagicMock()
        mock_aio_models.generate_content_stream = AsyncMock(return_value=fake_stream())
        mock_client_instance.aio.models = mock_aio_models

        events = [
            event
            async for event in provider.stream(
                request,
                model_name="gemini-2.5-flash",
            )
        ]

        assert events[0] == TextDelta("grounded")
        config = mock_aio_models.generate_content_stream.call_args.kwargs["config"]
        tool = config.tools[0]
        assert getattr(tool, "google_search", None) is not None
        assert getattr(tool, "google_search_retrieval", None) is None


@pytest.mark.asyncio
async def test_typed_stream_emits_text_then_exact_completion_metadata():
    """Gemini metadata travels in the terminal event, never through global context."""
    from types import SimpleNamespace

    from app.providers.stream_types import (
        FinishKind,
        GenerationRequest,
        GroundingMode,
        PromptRole,
        PromptTurn,
        StreamCompleted,
        TextDelta,
        TextPart,
    )

    _gemini_clients_cache.clear()
    grounding = SimpleNamespace(
        grounding_chunks=[
            SimpleNamespace(web=SimpleNamespace(uri="https://example.com", title="Example"))
        ]
    )

    class FakeChunk:
        text = "grounded answer"
        candidates = [
            SimpleNamespace(finish_reason="STOP", grounding_metadata=grounding)
        ]
        usage_metadata = SimpleNamespace(
            prompt_token_count=4,
            candidates_token_count=6,
            total_token_count=10,
            cached_content_token_count=None,
        )

    async def fake_stream():
        yield FakeChunk()

    request = GenerationRequest(
        models=("gemini-2.5-flash",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("question"),)),),
        grounding=GroundingMode.PROVIDER_SEARCH,
    )

    with (
        patch("app.providers.gemini.genai.Client") as client_class,
        patch("app.providers.gemini.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []
        provider = GeminiProvider("key")
        client = client_class.return_value
        client.aio.models.generate_content_stream = AsyncMock(return_value=fake_stream())

        events = [event async for event in provider.stream(request, model_name="gemini-2.5-flash")]

    assert events[0] == TextDelta("grounded answer")
    assert isinstance(events[1], StreamCompleted)
    completed = events[1]
    assert completed.finish_reason.kind is FinishKind.STOP
    assert completed.usage.total == 10
    assert completed.usage.cached is None
    assert completed.grounding.sources[0].url == "https://example.com"
    assert completed.route.actual_model == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_typed_inline_stream_uses_inline_timeout_budget():
    from app.providers.stream_types import (
        GenerationRequest,
        PromptRole,
        PromptTurn,
        TextPart,
        Workload,
    )

    _gemini_clients_cache.clear()

    async def fake_stream():
        yield type(
            "Chunk",
            (),
            {"text": "answer", "candidates": [], "usage_metadata": None},
        )()

    request = GenerationRequest(
        models=("gemini-3.1-flash-lite",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("question"),)),),
        workload=Workload.INLINE,
    )
    with (
        patch("app.providers.gemini.genai.Client"),
        patch("app.providers.gemini.settings") as mock_settings,
        patch("app.providers.gemini.asyncio.wait_for", new_callable=AsyncMock) as wait_for,
    ):
        mock_settings.SAFETY_SETTINGS = []
        wait_for.return_value = fake_stream()
        provider = GeminiProvider("key")
        events = [
            event
            async for event in provider.stream(
                request,
                model_name="gemini-3.1-flash-lite",
            )
        ]

    assert events
    assert wait_for.await_args.kwargs["timeout"] == 45.0


@pytest.mark.asyncio
async def test_typed_stream_prefers_structured_400_over_incidental_503_text():
    """A diagnostic id containing 503 must not turn a structured 400 into overload."""
    from app.errors import ErrorCode
    from app.providers.stream_types import (
        GenerationRequest,
        PromptRole,
        PromptTurn,
        StreamFailed,
        TextPart,
    )

    class StructuredBadRequest(Exception):
        code = 400

        def __str__(self) -> str:
            return "invalid request (debug object id 503123)"

    request = GenerationRequest(
        models=("gemini-3.1-flash-lite",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("question"),)),),
    )
    provider = GeminiProvider("key")
    provider._client.aio.models.generate_content_stream = AsyncMock(
        side_effect=StructuredBadRequest()
    )

    events = [
        event
        async for event in provider.stream(
            request,
            model_name="gemini-3.1-flash-lite",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], StreamFailed)
    assert events[0].code is ErrorCode.INVALID_REQUEST


def test_vertex_billing_disabled_error_temporarily_disables_vertex(monkeypatch):
    from app.providers import gemini

    monkeypatch.setattr(gemini, "_vertex_client", object())
    monkeypatch.setattr(gemini, "_vertex_client_initialized", True)
    monkeypatch.setattr(gemini, "_vertex_disabled_until_monotonic", 0.0, raising=False)
    monkeypatch.setattr(gemini.time, "monotonic", lambda: 100.0)

    assert gemini.is_vertex_client_available() is True

    gemini.report_vertex_error(
        RuntimeError(
            "403 PERMISSION_DENIED. This API method requires billing to be enabled. "
            "reason: BILLING_DISABLED"
        ),
        cooldown_seconds=3600.0,
    )

    assert gemini.is_vertex_client_available() is False

    monkeypatch.setattr(gemini.time, "monotonic", lambda: 3701.0)
    assert gemini.is_vertex_client_available() is True


@pytest.mark.asyncio
async def test_model_capability_validator_accepts_generate_content_model():
    from types import SimpleNamespace

    from app.providers import gemini

    client = MagicMock()
    client.aio.models.get = AsyncMock(
        return_value=SimpleNamespace(supported_actions=["generateContent"])
    )

    result = await gemini.validate_gemini_chat_model_capability(
        "gemini-3.7-flash",
        api_keys=["key"],
        client_factory=lambda _key: client,
    )

    assert result is gemini.GeminiModelValidationStatus.SUPPORTED
    client.aio.models.get.assert_awaited_once_with(model="gemini-3.7-flash")


@pytest.mark.asyncio
async def test_model_capability_validator_rejects_non_chat_model():
    from types import SimpleNamespace

    from app.providers import gemini

    client = MagicMock()
    client.aio.models.get = AsyncMock(return_value=SimpleNamespace(supported_actions=["embedContent"]))

    result = await gemini.validate_gemini_chat_model_capability(
        "gemini-embedding-model",
        api_keys=["key"],
        client_factory=lambda _key: client,
    )

    assert result is gemini.GeminiModelValidationStatus.UNSUPPORTED


@pytest.mark.asyncio
async def test_model_capability_validator_treats_404_as_unsupported():
    from app.providers import gemini

    class NotFoundError(Exception):
        code = 404

    client = MagicMock()
    client.aio.models.get = AsyncMock(side_effect=NotFoundError("not found"))

    result = await gemini.validate_gemini_chat_model_capability(
        "gemini-missing",
        api_keys=["key"],
        client_factory=lambda _key: client,
    )

    assert result is gemini.GeminiModelValidationStatus.UNSUPPORTED


@pytest.mark.asyncio
async def test_model_capability_validator_rotates_keys_and_reports_unavailable():
    from app.providers import gemini

    unavailable_client = MagicMock()
    unavailable_client.aio.models.get = AsyncMock(side_effect=RuntimeError("network unavailable"))

    result = await gemini.validate_gemini_chat_model_capability(
        "gemini-3.7-flash",
        api_keys=["key-1", "key-2"],
        client_factory=lambda _key: unavailable_client,
    )

    assert result is gemini.GeminiModelValidationStatus.UNAVAILABLE
    assert unavailable_client.aio.models.get.await_count == 2
