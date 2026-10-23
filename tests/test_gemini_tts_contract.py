"""Actual TTS payloads, complete audio and conservative input boundaries."""

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

    assert len(requests) == 1
    assert requests[0].url.path == "/v1beta/interactions"
    payload = json.loads(requests[0].content)
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
async def test_credentials_error_reaches_key_owner_unchanged(isolated_speech, monkeypatch):
    from google.genai.errors import ClientError

    tts, attempt = isolated_speech
    error = ClientError(401, {"error": {"message": "API key invalid", "status": "UNAUTHENTICATED"}})
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


@pytest.mark.parametrize(
    "text", ["слово " * 1000, "界" * 1000, "🐱" * 1000, "a" * 2000], ids=["cyrillic", "cjk", "emoji", "long-token"]
)
def test_long_sentences_and_tokens_obey_utf8_chunk_bound_without_losing_text(text):
    from app.providers.tts import _chunk_text_by_sentences

    chunks = _chunk_text_by_sentences(text, max_bytes=160)
    assert all(0 < len(chunk.encode()) <= 160 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_local_chunk_bound_is_rejected(limit):
    from app.providers.tts import _chunk_text_by_sentences

    with pytest.raises(ValueError):
        _chunk_text_by_sentences("Hello.", max_bytes=limit)


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
