"""Media process overrides retain specialized provider and quota paths."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.process_policies import ResolvedPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("gemini",), ("gemini", "pollinations")])
async def test_asr_provider_order_does_not_add_hidden_provider(monkeypatch, order):
    from app.utils import multimodal_processor as media

    monkeypatch.setattr(media, "resolve_process", AsyncMock(return_value=ResolvedPolicy(order, "sequential", True, 4)))
    gemini = AsyncMock(return_value=(None, "conversational", None))
    pollinations = AsyncMock(return_value=("Whisper result", "search", None))
    monkeypatch.setattr(media, "_transcribe_gemini", gemini)
    monkeypatch.setattr(media, "_transcribe_pollinations", pollinations)
    result = await media.transcribe_voice(b"ogg")
    gemini.assert_awaited_once()
    assert pollinations.await_count == ("pollinations" in order)
    assert result[0] == ("Whisper result" if "pollinations" in order else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process,method,payload",
    [
        ("media.memory_voice", "_transcribe_voice_for_ltm", b"ogg"),
        ("media.memory_image", "describe_image", b"png"),
        ("media.memory_document", "summarize_document_text", "document text " * 10),
    ],
)
async def test_memory_media_uses_own_exact_model_plan(monkeypatch, process, method, payload):
    from app.runtime_settings import result_execution
    from app.utils import multimodal_processor as media

    async def resolve(process_id, baseline):
        assert process_id == process
        return ResolvedPolicy(("gemini-first", "gemini-second"), "sequential", True, 7)

    monkeypatch.setattr(result_execution, "resolve_process", resolve)
    generate = AsyncMock(side_effect=[None, "enriched result"])
    monkeypatch.setattr(media, "_generate_with_resilience", generate)
    assert await getattr(media, method)(payload) == "enriched result"
    assert [call.kwargs["model"] for call in generate.await_args_list] == ["gemini-first", "gemini-second"]
    assert generate.await_args_list[0].kwargs["system_prompt"] == generate.await_args_list[1].kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_tts_explicit_models_keep_elevenlabs_priority_and_gemini_order(monkeypatch):
    from app import voice_engine
    from app.config import settings
    from app.providers import elevenlabs_tts
    from app.utils import audio

    manager = voice_engine.VoiceReplyManager()
    job = voice_engine.VoiceJob(
        job_id="job",
        user_id=1,
        chat_id=1,
        reply_to_message_id=1,
        response_text="Привет мир.",
        voice="Aoede",
        tts_temperature=None,
        source_key="source",
        bot=MagicMock(),
        expected_epoch=0,
    )
    attempted = []

    async def resolve(process_id, baseline):
        if process_id == "tts.delivery":
            return ResolvedPolicy(baseline)
        assert process_id == "tts"
        assert baseline == ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
        return ResolvedPolicy(("gemini-admin-a-tts", "gemini-admin-b-tts"), "sequential", True, 7)

    async def gemini(chunks, voice, timeout, **kwargs):
        attempted.append(kwargs["model_name"])
        return [b"pcm"] if len(attempted) == 2 else None

    monkeypatch.setattr(voice_engine, "resolve_process", resolve, raising=False)
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEYS", ["key"])
    monkeypatch.setattr(elevenlabs_tts, "generate_speech_with_key_rotation", AsyncMock(return_value=None))
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", gemini)
    monkeypatch.setattr(audio, "crossfade_pcm_chunks", lambda parts: b"pcm")
    monkeypatch.setattr(audio, "pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))

    assert await manager._pregenerate_audio(job) == b"ogg"
    assert attempted == ["gemini-admin-a-tts", "gemini-admin-b-tts"]


@pytest.mark.asyncio
async def test_asr_explicit_plan_excludes_legacy_hidden_fallback(monkeypatch):
    from app.providers import pollinations
    from app.utils import multimodal_processor as media

    attempted = []

    async def resolve(process_id, baseline):
        if process_id == "asr.delivery":
            return ResolvedPolicy(baseline)
        assert process_id == "asr"
        assert baseline == (media.TRANSCRIPTION_MODEL, "gemini-3.5-flash")
        return ResolvedPolicy(("gemini-admin-a", "gemini-admin-b"), "sequential", True, 3)

    async def generate(*, model, **kwargs):
        attempted.append(model)
        return "spoken text\nINTENT:CONVERSATIONAL" if len(attempted) == 2 else None

    monkeypatch.setattr(media, "resolve_process", resolve, raising=False)
    monkeypatch.setattr(
        pollinations,
        "get_pollinations_provider",
        lambda: SimpleNamespace(transcribe_audio=AsyncMock(return_value=None)),
    )
    monkeypatch.setattr(media, "_generate_with_resilience", generate)

    assert await media.transcribe_voice(b"ogg") == ("spoken text", "conversational", None)
    assert attempted == ["gemini-admin-a", "gemini-admin-b"]


@pytest.mark.asyncio
async def test_media_intent_uses_exact_explicit_chain(monkeypatch):
    from google.genai import types

    from app.utils import multimodal_processor as media

    attempted = []

    async def resolve(process_id, baseline):
        assert process_id == "media.intent"
        assert baseline == tuple(media._INTENT_MODEL_CHAIN)
        return ResolvedPolicy(("gemini-admin-a", "gemini-admin-b"), "sequential", True, 4)

    async def generate(*, model, **kwargs):
        attempted.append(model)
        return "INTENT:SEARCH" if len(attempted) == 2 else None

    monkeypatch.setattr(media, "resolve_process", resolve)
    monkeypatch.setattr(media, "_generate_with_resilience", generate)
    result = await media._classify_intent_with_fallback([types.Part.from_text(text="weather")])
    assert result == "INTENT:SEARCH"
    assert attempted == ["gemini-admin-a", "gemini-admin-b"]


@pytest.mark.asyncio
async def test_live_policy_applies_to_new_ai_studio_session_only(monkeypatch):
    from app import web_miniapp as web
    from app.providers import gemini

    async def resolve(process_id, baseline):
        if process_id == "live.vertex":
            return ResolvedPolicy(baseline)
        assert process_id == "live"
        return ResolvedPolicy(("gemini-admin-live",), "sequential", True, 5)

    monkeypatch.setattr(web, "resolve_process", resolve, raising=False)
    monkeypatch.setattr(gemini, "get_live_api_client", lambda: object())
    monkeypatch.setattr(gemini, "get_vertex_live_client", lambda: object())
    monkeypatch.setattr(web, "_get_live_model_cooldown_seconds", AsyncMock(return_value=0))

    _, studio_model, _, error, _ = await web._resolve_live_transport(
        transport_mode=web._LIVE_DEFAULT_CONNECTION_MODE,
        system_instruction="instruction",
        resumption_handle=None,
        voice_name="Aoede",
        thinking_level="low",
    )
    assert error is None
    assert studio_model == "gemini-admin-live"

    _, vertex_model, _, error, _ = await web._resolve_live_transport(
        transport_mode=web._LIVE_VERTEX_CONNECTION_MODE,
        system_instruction="instruction",
        resumption_handle=None,
        voice_name="Aoede",
        thinking_level="low",
    )
    assert error is None
    assert vertex_model == web._VERTEX_LIVE_MODEL


@pytest.mark.asyncio
async def test_image_explicit_chain_charges_user_quota_once(monkeypatch):
    from app.providers import imagen_provider

    attempted = []

    async def resolve(process_id, baseline):
        assert process_id == "image.gemini"
        assert baseline == (imagen_provider.GEMINI_IMAGE_MODEL,)
        return ResolvedPolicy(("gemini-a-image", "gemini-b-image"), "sequential", True, 8)

    async def generate_one(self, prompt, model, aspect_ratio, number_of_images, user_id, **kwargs):
        attempted.append((model, kwargs["_consume_quota"], kwargs["_honor_model"]))
        return imagen_provider.ImageGenResult(success=len(attempted) == 2, error_message="overloaded")

    monkeypatch.setattr(imagen_provider, "resolve_process", resolve)
    monkeypatch.setattr(imagen_provider.ImagenProvider, "_generate_one_model", generate_one)
    result = await imagen_provider.ImagenProvider().generate("a cat", user_id=1)
    assert result.success
    assert attempted == [("gemini-a-image", True, True), ("gemini-b-image", False, True)]


@pytest.mark.asyncio
async def test_crocodile_plan_tries_every_configured_model(monkeypatch):
    from app.games import daily_ai

    attempted = []

    async def resolve(process_id, baseline):
        assert process_id == "crocodile.words"
        return ResolvedPolicy(("gemini-a", "gemini-b"), "sequential", True, 9)

    async def generate(prompt, model, timeout=30):
        attempted.append(model)
        if model == "gemini-a":
            raise RuntimeError("provider unavailable")
        return '{"word":"кот"}'

    monkeypatch.setattr(daily_ai, "resolve_process", resolve)
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await daily_ai.generate_daily_text_for("words", "prompt", "gemini-baseline") == '{"word":"кот"}'
    assert attempted == ["gemini-a", "gemini-b"]


@pytest.mark.asyncio
async def test_image_prompt_translation_uses_exact_text_model_plan(monkeypatch):
    from app.config import settings
    from app.handlers import cmd_image
    from app.observability import workload_events
    from app.providers import gemini

    attempted = []

    async def resolve(process_id, baseline):
        assert (process_id, baseline) == ("image.translate", (cmd_image._TRANSLATE_MODEL,))
        return ResolvedPolicy(("gemini-a", "gemini-b"), "sequential", True, 6)

    async def generate(*, model, **kwargs):
        attempted.append(model)
        if model == "gemini-a":
            raise RuntimeError("unavailable")
        return SimpleNamespace(text="a painted cat")

    async def observe(awaitable, **kwargs):
        return await awaitable

    monkeypatch.setattr(settings, "GEMINI_API_KEYS", ["test-key"])
    monkeypatch.setattr("app.runtime_settings.gemini_execution.resolve_process", resolve)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": "test-hash"}),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr(cmd_image.metrics_collector, "record_api_call", AsyncMock())
    monkeypatch.setattr(workload_events, "observe_workload_call", observe)
    monkeypatch.setattr(
        gemini,
        "get_cached_genai_client",
        lambda key: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
    )

    assert await cmd_image._translate_to_english("кот") == "a painted cat"
    assert attempted == ["gemini-a", "gemini-b"]
