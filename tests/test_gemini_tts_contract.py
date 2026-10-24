"""Actual TTS payloads, complete audio and model-native input boundaries."""

import base64
import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google import genai
from google.genai import types

PCM = b"\x01\x00" * 120


@pytest.fixture
def isolated_speech(monkeypatch):
    from app.providers import tts
    from app.runtime_settings import lifecycle, store

    snapshot = store.SettingsSnapshot(0, MappingProxyType({}))
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(
        tts,
        "get_config",
        AsyncMock(
            return_value={
                "default_voice": "Aoede",
                "voices": [{"id": "Aoede", "name": "Default", "prompt": "Speak softly."}],
                "temperature": 0.2,
                "max_output_tokens": 4096,
            }
        ),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    attempt = MagicMock()
    monkeypatch.setattr(tts, "start_workload_attempt", MagicMock(return_value=attempt))
    return tts, attempt


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "gemini-3.8-flash-tts"])
async def test_current_speech_uses_actual_sdk_metadata_pcm_and_storage_opt_out(isolated_speech, monkeypatch, model):
    tts, attempt = isolated_speech
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 40})
        return httpx.Response(
            200,
            json={
                "id": "offline-interaction",
                "status": "completed",
                "model": "gemini-3.8-flash-lite-tts",
                "steps": [
                    {
                        "type": "model_output",
                        "role": "model",
                        "content": [
                            {"type": "audio", "mime_type": "audio/l16", "data": base64.b64encode(PCM).decode()}
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline-test-key", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            kwargs = {"model_name": model} if model is not None else {}
            assert await tts.generate_speech("**Hello**. Read this only.", "offline-test-key", **kwargs) == PCM
        finally:
            await client.aio.aclose()
            client.close()

    assert len(requests) == 2
    assert requests[0].url.path.endswith(f"/{model or 'gemini-3.8-flash-lite-tts'}:countTokens")
    assert requests[1].url.path == "/v1beta/interactions"
    payload = json.loads(requests[1].content)
    assert payload["model"] == (model or "gemini-3.8-flash-lite-tts")
    assert payload["store"] is False
    assert payload["response_format"] == {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000}
    content = payload["input"][0]["content"][0]
    assert content["text"] == "Hello. Read this only."
    assert content["annotations"][0]["type"] == "speech_metadata"
    assert "Speak softly." in content["annotations"][0]["style"]
    assert payload["generation_config"]["speech_config"] == [{"voice": "Aoede"}]
    assert payload["generation_config"]["max_output_tokens"] == 4096
    assert "temperature" not in payload["generation_config"]
    attempt.finish.assert_called_once()
    assert attempt.finish.call_args.kwargs["outcome"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["MAX_TOKENS", "SAFETY", None])
async def test_legacy_nonempty_but_incomplete_audio_is_rejected(isolated_speech, monkeypatch, finish_reason):
    from app.providers.tts_audio import TTSOutputError

    tts, attempt = isolated_speech
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason=finish_reason,
                content=types.Content(
                    parts=[types.Part(inline_data=types.Blob(data=PCM, mime_type="audio/l16;rate=24000"))]
                ),
            )
        ]
    )
    generate = AsyncMock(return_value=response)
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
    )
    with pytest.raises(TTSOutputError):
        await tts.generate_speech("Hello.", "offline-test-key", model_name="gemini-3.1-flash-tts-preview")
    assert not any(call.kwargs.get("outcome") == "succeeded" for call in attempt.finish.call_args_list)


@pytest.mark.asyncio
async def test_legacy_audio_parts_are_all_normalized_and_kept_in_order(isolated_speech, monkeypatch):
    tts, _ = isolated_speech
    second = b"\x02\x00" * 120
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason="STOP",
                content=types.Content(
                    parts=[
                        types.Part(inline_data=types.Blob(data=part, mime_type="audio/l16;rate=24000"))
                        for part in (PCM, second)
                    ]
                ),
            )
        ]
    )
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock(return_value=response)))
        ),
    )
    assert (
        await tts.generate_speech("Hello.", "offline-test-key", model_name="gemini-3.1-flash-tts-preview")
        == PCM + second
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "API_KEY_INVALID"),
        (401, "API_KEY_INVALID"),
        (400, "API key not valid"),
        (400, "FAILED_PRECONDITION: free tier unavailable"),
    ],
)
async def test_credentials_error_reaches_key_owner_unchanged(isolated_speech, monkeypatch, status, message):
    from google.genai.errors import ClientError

    tts, attempt = isolated_speech
    error = ClientError(status, {"error": {"message": message, "status": "INVALID_ARGUMENT"}})
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock(side_effect=error)))
        ),
    )
    with pytest.raises(ClientError) as caught:
        await tts.generate_speech("Hello.", "offline-test-key", model_name="gemini-3.1-flash-tts-preview")
    assert caught.value is error
    attempt.fail.assert_called_once()


@pytest.mark.parametrize("status", ["failed", "incomplete", "budget_exceeded", "in_progress", "cancelled"])
def test_current_audio_requires_completed_interaction(status):
    from app.providers.tts import _interaction_audio
    from app.providers.tts_audio import TTSOutputError

    with pytest.raises(TTSOutputError, match="incomplete_audio"):
        _interaction_audio(SimpleNamespace(status=status, steps=[]))


@pytest.mark.parametrize("problem", ["encoding", "mime", "sample_rate", "alignment", "step_error", "diagnostic"])
def test_current_audio_validates_every_output_part(problem):
    from app.providers.tts import _interaction_audio
    from app.providers.tts_audio import TTSOutputError

    first = SimpleNamespace(type="audio", mime_type="audio/l16", data=base64.b64encode(PCM).decode())
    second = SimpleNamespace(type="audio", mime_type="audio/l16", data=base64.b64encode(PCM).decode())
    step = SimpleNamespace(type="model_output", content=[first, second], error=None)
    response = SimpleNamespace(status="completed", steps=[step], errors=None)
    if problem == "encoding":
        second.data = "!invalid!"
    elif problem == "mime":
        second.mime_type = "application/json"
    elif problem == "sample_rate":
        second.mime_type = "audio/l16;rate=48000"
    elif problem == "alignment":
        second.data = base64.b64encode(b"x").decode()
    elif problem == "step_error":
        step.error = SimpleNamespace(code=13)
    else:
        response.errors = [SimpleNamespace(code=13)]
    with pytest.raises(TTSOutputError):
        _interaction_audio(response)


def test_current_audio_keeps_all_steps_in_order():
    from app.providers.tts import _interaction_audio

    steps = [
        SimpleNamespace(
            type="model_output",
            error=None,
            content=[SimpleNamespace(type="audio", mime_type="audio/l16", data=base64.b64encode(part).decode())],
        )
        for part in (PCM, b"\x02\x00" * 60)
    ]
    assert _interaction_audio(SimpleNamespace(status="completed", steps=steps, errors=None)) == PCM + b"\x02\x00" * 60


@pytest.mark.asyncio
async def test_oversized_director_prompt_is_rejected_before_key_reservation(isolated_speech, monkeypatch):
    from app.providers.tts_audio import TTSRequestError

    tts, attempt = isolated_speech
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(tts, "_get_delivery_direction", lambda *_: "x" * 7501)
    with pytest.raises(TTSRequestError, match="input_too_large"):
        await tts.generate_speech("Hello.", "offline-test-key")
    reserve.assert_not_awaited()
    attempt.finish.assert_not_called()


@pytest.mark.asyncio
async def test_source_memory_guard_is_rejected_locally_before_any_quota_or_sdk(isolated_speech, monkeypatch):
    from app.providers.tts_audio import TTSRequestError

    tts, attempt = isolated_speech
    reserve = AsyncMock(return_value=True)
    sdk = MagicMock(
        return_value=SimpleNamespace(
            aio=SimpleNamespace(
                interactions=SimpleNamespace(
                    create=AsyncMock(
                        return_value=SimpleNamespace(
                            status="completed",
                            errors=None,
                            steps=[
                                SimpleNamespace(
                                    type="model_output",
                                    error=None,
                                    content=[
                                        SimpleNamespace(
                                            type="audio", mime_type="audio/l16", data=base64.b64encode(PCM).decode()
                                        )
                                    ],
                                )
                            ],
                        )
                    )
                )
            )
        )
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(tts, "get_cached_genai_client", sdk)
    with pytest.raises(TTSRequestError, match="input_too_large"):
        await tts.generate_speech("я" * 32769, "offline-test-key")
    reserve.assert_not_awaited()
    sdk.assert_not_called()
    attempt.finish.assert_not_called()


@pytest.mark.asyncio
async def test_direction_metadata_consumes_the_same_input_allowance(isolated_speech, monkeypatch):
    from app.providers.tts_audio import TTSRequestError

    tts, _ = isolated_speech
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(tts, "_get_delivery_direction", lambda *_: "я" * 3500)
    count = AsyncMock(return_value=SimpleNamespace(total_tokens=9000))
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count))),
    )
    with pytest.raises(TTSRequestError, match="input_too_large"):
        await tts.generate_speech("x" * 200, "offline-test-key")
    reserve.assert_not_awaited()
    counted = count.call_args.kwargs["contents"].parts
    assert any("я" * 3500 in part.text for part in counted)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview"])
async def test_completed_audio_at_output_token_ceiling_is_never_accepted(isolated_speech, monkeypatch, model):
    from app.providers.tts_audio import TTSOutputError

    tts, _ = isolated_speech
    if model.startswith("gemini-3.8"):
        payload = {
            "id": "offline",
            "status": "completed",
            "model": model,
            "usage": {"total_output_tokens": 4096},
            "steps": [
                {
                    "type": "model_output",
                    "role": "model",
                    "content": [
                        {
                            "type": "audio",
                            "mime_type": "audio/l16",
                            "data": base64.b64encode(PCM).decode(),
                        }
                    ],
                }
            ],
        }
    else:
        payload = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "inlineData": {"mimeType": "audio/l16", "data": base64.b64encode(PCM).decode()},
                            }
                        ],
                    },
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 4096},
        }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as transport:
        client = genai.Client(api_key="offline-test-key", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            with pytest.raises(TTSOutputError, match="output_token_limit"):
                await tts.generate_speech("Complete source.", "offline-test-key", model_name=model)
        finally:
            await client.aio.aclose()
            client.close()


@pytest.mark.asyncio
async def test_provider_input_token_error_has_a_resizable_contract_not_a_key_error(isolated_speech, monkeypatch):
    from google.genai.errors import ClientError

    from app.providers.tts_audio import TTSRequestError

    tts, _ = isolated_speech
    error = ClientError(400, {"error": {"message": "Input token count exceeds maximum input token limit of 8192"}})
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=AsyncMock(side_effect=error)))
        ),
    )
    with pytest.raises(TTSRequestError, match="input_too_large"):
        await tts.generate_speech("Complete source.", "offline-test-key")


@pytest.mark.asyncio
async def test_actual_sdk_incomplete_response_is_subdivided_and_each_dispatch_is_accounted(
    isolated_speech, monkeypatch
):
    from app import voice_engine

    tts, _ = isolated_speech
    text = "First complete transcript. Second complete transcript."
    requests = []
    reserve = AsyncMock(return_value=True)
    suspend = AsyncMock()
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"key_hash": "offline", "api_key": "offline-test-key"}),
    )
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))

    def respond(request):
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 40})
        transcript = json.loads(request.content)["input"][0]["content"][0]["text"]
        requests.append(transcript)
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "status": "incomplete" if transcript == text else "completed",
                "model": "gemini-3.8-flash-lite-tts",
                "steps": [
                    {
                        "type": "model_output",
                        "role": "model",
                        "content": [
                            {
                                "type": "audio",
                                "mime_type": "audio/l16",
                                "data": base64.b64encode(PCM).decode(),
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline-test-key", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            prepared = await tts.prepare_speech_settings(voice="Aoede")
            result = await voice_engine._run_gemini_pipeline([text], "Aoede", 1, speech_settings=prepared)
        finally:
            await client.aio.aclose()
            client.close()
    assert result == [PCM, PCM]
    assert requests == [text, "First complete transcript. ", "Second complete transcript."]
    assert reserve.await_count == 3
    suspend.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_separator_pieces_do_not_abort_real_sdk_synthesis(isolated_speech, monkeypatch):
    from app import voice_engine

    tts, _ = isolated_speech
    tts.get_config.return_value["max_output_tokens"] = 1024
    text = "First. " + " " * 400 + "Last."
    requests, progress = [], []
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"key_hash": "offline", "api_key": "offline-test-key"}),
    )
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))

    async def complete(done, total):
        progress.append((done, total))

    def respond(request):
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 40})
        transcript = json.loads(request.content)["input"][0]["content"][0]["text"]
        requests.append(transcript)
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "status": "completed",
                "model": "gemini-3.8-flash-lite-tts",
                "steps": [
                    {
                        "type": "model_output",
                        "role": "model",
                        "content": [
                            {
                                "type": "audio",
                                "mime_type": "audio/l16",
                                "data": base64.b64encode(PCM).decode(),
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline-test-key", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            prepared = await tts.prepare_speech_settings(voice="Aoede")
            pieces = ["First. ", " " * 400, "Last."]
            assert "".join(pieces) == text
            result = await voice_engine._run_gemini_pipeline(
                pieces,
                "Aoede",
                1,
                speech_settings=prepared,
                on_chunk_complete=complete,
            )
        finally:
            await client.aio.aclose()
            client.close()
    assert result == [PCM, PCM]
    assert [item.strip() for item in requests] == ["First.", "Last."]
    assert reserve.await_count == 2
    assert progress[-1] == (2, 2)


@pytest.mark.asyncio
async def test_provider_deadline_has_one_failed_terminal(isolated_speech, monkeypatch):
    import asyncio

    tts, attempt = isolated_speech

    async def blocked(**_):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(interactions=SimpleNamespace(create=blocked))),
    )
    with pytest.raises(TimeoutError):
        await tts.generate_speech("Hello.", "offline-test-key", timeout=0.01)
    attempt.fail.assert_called_once()
    assert attempt.fail.call_args.kwargs["reason_code"] == "timeout"
    attempt.finish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", ["Aoede", "Kore", "Puck", "Charon", "Leda", "Orus", "Zephyr", "Rasalgethi"])
async def test_current_voices_send_brief_individual_delivery_without_legacy_reading_instructions(
    isolated_speech, monkeypatch, voice
):
    from app.runtime_settings.audio import baseline_config

    tts, _ = isolated_speech
    monkeypatch.setattr(tts, "get_config", AsyncMock(return_value=baseline_config("gemini")))
    seen = []

    async def synthesize(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            status="completed",
            errors=None,
            steps=[
                SimpleNamespace(
                    type="model_output",
                    error=None,
                    content=[SimpleNamespace(type="audio", mime_type="audio/l16", data=base64.b64encode(PCM).decode())],
                )
            ],
        )

    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(interactions=SimpleNamespace(create=synthesize))),
    )
    assert await tts.generate_speech("Прочитай только это предложение.", "offline-test-key", voice=voice) == PCM
    content = seen[0]["input"][0]["content"][0]
    assert content["text"] == "Прочитай только это предложение."
    style = content["annotations"][0]["style"]
    individual = next(row for row in baseline_config("gemini")["voices"] if row["id"] == voice)
    assert individual["prompt"] and individual["prompt"] in style
    assert 0.5 <= individual["temperature"] <= 1.0
    assert len(style) < 350
    assert "Text to read" not in style
    assert "Pronunciation Rules" not in style
    assert "temperature" not in seen[0]["generation_config"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("personal", "expected"), [(None, 0.7), (0.9, 0.9)])
async def test_legacy_voice_level_is_used_unless_user_overrides_it(isolated_speech, monkeypatch, personal, expected):
    tts, _ = isolated_speech
    config = await tts.get_config("gemini")
    config["voices"][0]["temperature"] = 0.7
    seen = []

    async def synthesize(**kwargs):
        seen.append(kwargs)
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    finish_reason="STOP",
                    content=types.Content(
                        parts=[types.Part(inline_data=types.Blob(data=PCM, mime_type="audio/l16;rate=24000"))]
                    ),
                )
            ]
        )

    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=synthesize))),
    )
    assert (
        await tts.generate_speech(
            "Hello.", "offline-test-key", model_name="gemini-3.1-flash-tts-preview", tts_temperature=personal
        )
        == PCM
    )
    assert seen[0]["config"].temperature == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "provider_error", "cancelled"])
async def test_sent_tts_requests_keep_minute_and_daily_usage(isolated_speech, monkeypatch, outcome):
    import asyncio

    from app.config import settings

    tts, _ = isolated_speech
    monkeypatch.setattr(settings, "GEMINI_TTS_RPM_PER_KEY", 1)
    daily = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", daily)
    sent = []

    async def synthesize(**kwargs):
        sent.append(kwargs["input"][0]["content"][0]["text"])
        if outcome == "provider_error":
            raise OSError("offline")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return SimpleNamespace(
            status="completed",
            errors=None,
            steps=[
                SimpleNamespace(
                    type="model_output",
                    content=[SimpleNamespace(type="audio", mime_type="audio/l16", data=base64.b64encode(PCM).decode())],
                )
            ],
        )

    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(interactions=SimpleNamespace(create=synthesize))),
    )
    if outcome == "success":
        assert await tts.generate_speech("Whole transcript.", "key") == PCM
    else:
        with pytest.raises(OSError if outcome == "provider_error" else asyncio.CancelledError):
            await tts.generate_speech("Whole transcript.", "key")
    with pytest.raises(tts.TTSAdmissionRejected) as rejected:
        await tts.generate_speech("Whole transcript.", "key")
    assert rejected.value.retry_after is not None and 0 < rejected.value.retry_after <= 60
    assert sent == ["Whole transcript."]
    daily.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [False, OSError("database unavailable"), "cancelled"])
async def test_unsent_daily_refusal_refunds_minute_slot(isolated_speech, monkeypatch, refusal):
    import asyncio

    from app.config import settings

    tts, _ = isolated_speech
    monkeypatch.setattr(settings, "GEMINI_TTS_RPM_PER_KEY", 1)
    daily = AsyncMock(side_effect=[asyncio.CancelledError() if refusal == "cancelled" else refusal, True])
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", daily)
    response = SimpleNamespace(
        status="completed",
        errors=None,
        steps=[
            SimpleNamespace(
                type="model_output",
                content=[SimpleNamespace(type="audio", mime_type="audio/l16", data=base64.b64encode(PCM).decode())],
            )
        ],
    )
    sdk = AsyncMock(return_value=response)
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(interactions=SimpleNamespace(create=sdk))),
    )
    with pytest.raises(asyncio.CancelledError if refusal == "cancelled" else tts.TTSAdmissionRejected):
        await tts.generate_speech("Whole transcript.", "key")
    assert await tts.generate_speech("Whole transcript.", "key") == PCM
    sdk.assert_awaited_once()
    assert daily.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 429, 503])
@pytest.mark.parametrize("model", ["gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview"])
async def test_sdk_never_retries_beneath_tts_quota_accounting(isolated_speech, monkeypatch, status, model):
    from app.providers import gemini

    tts, _ = isolated_speech
    requests = []

    def respond(request):
        requests.append(request)
        if status == 0:
            raise httpx.ReadError("offline connection closed", request=request)
        code = (
            ("RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE")
            if "/interactions" in request.url.path
            else status
        )
        return httpx.Response(status, json={"error": {"code": code, "message": "offline failure"}})

    original_client = genai.Client
    clients = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:

        def client_factory(**kwargs):
            options = kwargs["http_options"].model_copy(update={"httpx_async_client": transport})
            client = original_client(api_key=kwargs["api_key"], http_options=options)
            clients.append(client)
            return client

        monkeypatch.setattr(gemini.genai, "Client", client_factory)
        monkeypatch.setattr(gemini, "_gemini_clients_cache", {})
        try:
            with pytest.raises(Exception) as caught:
                await tts.generate_speech("Whole transcript.", "single-attempt-key", model_name=model)
            if status:
                assert (getattr(caught.value, "status_code", None) or getattr(caught.value, "code", None)) == status
        finally:
            for client in clients:
                await client.aio.aclose()
                client.close()
    assert len(requests) == 2
    assert sum(request.url.path.endswith(":countTokens") for request in requests) == 1
