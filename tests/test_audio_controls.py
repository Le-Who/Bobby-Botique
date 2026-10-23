"""Offline voice catalog persistence and provider payload regressions."""

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runtime_settings import lifecycle, store


@pytest.mark.parametrize("configured", ["", " ", "\t\n"])
def test_empty_optional_elevenlabs_voice_uses_valid_baseline(monkeypatch, configured):
    from app.config import settings
    from app.runtime_settings import audio

    monkeypatch.setattr(settings, "ELEVENLABS_VOICE_ID", configured)
    config = audio.validate_config("elevenlabs", audio.baseline_config("elevenlabs"))
    assert config["default_voice"] == "XB0fDUnXU5powFXDhCwa"
    assert len(config["voices"]) == 7


@pytest.fixture
def audio_runtime(monkeypatch):
    from tests.test_runtime_settings_store import Database

    backend = store.RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", Database())
    for name in ("get_snapshot", "set_value", "reset_value"):
        monkeypatch.setattr(store, name, getattr(backend, name))
    return backend


def gemini_config():
    return {
        "default_voice": "FutureLongVoiceName",
        "voices": [
            {"id": "FutureLongVoiceName", "name": "Первый", "prompt": "Speak softly."},
            {"id": "Kore", "name": "Второй", "prompt": ""},
        ],
        "temperature": 0.2,
        "max_output_tokens": 4096,
    }


@pytest.mark.asyncio
async def test_voice_catalog_order_defaults_and_reset_are_durable(audio_runtime):
    from app.runtime_settings import audio

    await audio.save_config("gemini", gemini_config(), expected_revision=0, actor="admin")
    value = await audio.get_config("gemini")
    assert [row["id"] for row in value["voices"]] == ["FutureLongVoiceName", "Kore"]
    assert value["default_voice"] == "FutureLongVoiceName"
    value["voices"].clear()
    assert len((await audio.get_config("gemini"))["voices"]) == 2
    with pytest.raises(store.RevisionConflict):
        await audio.save_config("gemini", gemini_config(), expected_revision=0, actor="admin")
    await audio.reset_config("gemini", expected_revision=1, actor="admin")
    assert (await audio.get_config("gemini"))["default_voice"] == "Aoede"


@pytest.mark.parametrize(
    "invalid", ["duplicate", "missing_default", "empty", "nan", "bool", "path", "token_limit", "unknown"]
)
def test_invalid_audio_settings_are_rejected(invalid):
    from app.runtime_settings import audio

    value = gemini_config()
    if invalid == "duplicate":
        value["voices"].append(value["voices"][0])
    elif invalid == "missing_default":
        value["default_voice"] = "absent"
    elif invalid == "empty":
        value["voices"] = []
    elif invalid == "nan":
        value["temperature"] = float("nan")
    elif invalid == "bool":
        value["temperature"] = True
    elif invalid == "path":
        value["voices"][0]["id"] = "../voice"
    elif invalid == "token_limit":
        value["max_output_tokens"] = 16385
    else:
        value["api_key"] = "not-an-audio-setting"
    with pytest.raises(ValueError):
        audio.validate_config("gemini", value)


@pytest.mark.asyncio
async def test_catalog_uses_admin_provider_order_and_omits_private_director_text(audio_runtime, monkeypatch):
    from app.config import settings
    from app.runtime_settings import audio

    monkeypatch.setattr(settings, "ELEVENLABS_API_KEYS", ["fake-key"])
    await audio.save_config("gemini", gemini_config(), expected_revision=0, actor="admin")
    await audio_runtime.set_value(
        "process:tts.delivery",
        {"models": ["gemini", "elevenlabs"], "strategy": "sequential", "inherit_user_model": False},
        expected_revision=1,
        actor="admin",
    )
    result = await audio.user_voice_catalog()
    assert result == {
        "provider": "gemini",
        "default_voice": "FutureLongVoiceName",
        "voices": [
            {"id": "FutureLongVoiceName", "name": "Первый"},
            {"id": "Kore", "name": "Второй"},
        ],
    }
    assert await audio.valid_user_voice("FutureLongVoiceName")
    assert not await audio.valid_user_voice("removed")


@pytest.mark.asyncio
async def test_gemini_payload_uses_pinned_voice_prompt_and_admin_parameters(audio_runtime, monkeypatch):
    from app.providers import tts
    from app.runtime_settings import audio

    await audio.save_config("gemini", gemini_config(), expected_revision=0, actor="admin")
    first = await audio_runtime.get_snapshot()
    sent = []

    async def generate_content(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    finish_reason="STOP",
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(inline_data=SimpleNamespace(data=b"pc", mime_type="audio/l16;rate=24000"))
                        ]
                    ),
                )
            ]
        )

    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    async with lifecycle.runtime_settings_scope(first):
        changed = gemini_config()
        changed["voices"][0]["prompt"] = "Do not use this later revision."
        await audio.save_config("gemini", changed, expected_revision=1, actor="admin")
        assert (
            await tts.generate_speech(
                "Текст.", "fake-key", voice="FutureLongVoiceName", model_name="gemini-3.1-flash-tts-preview"
            )
            == b"pc"
        )
    payload = sent[0]
    assert "Speak softly." in payload["contents"]
    assert "Do not use this later revision." not in payload["contents"]
    assert payload["contents"].index("Speak softly.") < payload["contents"].index("Text to read:")
    assert payload["config"].temperature == 0.5  # Existing API clamp is preserved.
    assert payload["config"].max_output_tokens == 4096
    assert payload["config"].speech_config.voice_config.prebuilt_voice_config.voice_name == "FutureLongVoiceName"


@pytest.mark.asyncio
async def test_elevenlabs_payload_uses_admin_voice_parameters(audio_runtime, monkeypatch):
    from app.providers import elevenlabs_tts
    from app.runtime_settings import audio

    config = await audio.get_config("elevenlabs")
    config["voice_settings"] = {"stability": 0.1, "similarity_boost": 0.6, "style": 0.9, "use_speaker_boost": False}
    config["apply_text_normalization"] = "off"
    await audio.save_config("elevenlabs", config, expected_revision=0, actor="admin")
    sent = []

    import json

    import httpx

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=b"p" * 150, headers={"Content-Type": "audio/pcm;rate=24000"})

    async with httpx.AsyncClient(
        base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(respond)
    ) as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        assert await elevenlabs_tts.generate_speech_elevenlabs("Hello", "fake-key", voice_id=config["default_voice"])
    assert sent[0]["voice_settings"] == {
        "stability": 0.1,
        "similarity_boost": 0.6,
        "style": 0.9,
        "use_speaker_boost": False,
    }
    assert sent[0]["apply_text_normalization"] == "off"


@pytest.mark.asyncio
async def test_generation_uses_catalog_membership_and_fallback_default(audio_runtime, monkeypatch):
    from app import voice_engine
    from app.config import settings
    from app.runtime_settings import audio

    await audio.save_config("gemini", gemini_config(), expected_revision=0, actor="admin")
    el = await audio.get_config("elevenlabs")
    el["voices"] = [{"id": "short", "name": "Short ID"}]
    el["default_voice"] = "short"
    await audio.save_config("elevenlabs", el, expected_revision=1, actor="admin")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEYS", ["fake-key"])
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    calls = []

    async def el_generate(*args, voice_id, **kwargs):
        calls.append(("elevenlabs", voice_id))
        return None

    async def gemini_generate(*args, **kwargs):
        calls.append(("gemini", args[1]))
        return [b"pcm"]

    monkeypatch.setattr("app.providers.elevenlabs_tts.generate_speech_with_key_rotation", el_generate)
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", gemini_generate)
    monkeypatch.setattr("app.utils.audio.crossfade_pcm_chunks", lambda _: b"pcm")
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    job = SimpleNamespace(response_text="Hello.", voice="short", tts_temperature=None)
    manager = voice_engine.VoiceReplyManager()
    monkeypatch.setattr(manager, "_set_status", AsyncMock())
    assert await manager._pregenerate_audio(job) == b"ogg"
    assert calls == [("elevenlabs", "short"), ("gemini", "FutureLongVoiceName")]
