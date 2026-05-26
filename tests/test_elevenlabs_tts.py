# /tests/test_elevenlabs_tts.py
"""Unit tests for the ElevenLabs TTS provider HTTP layer, key rotation router, and voice fetcher."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.providers import elevenlabs_tts
from app.providers.elevenlabs_tts import (
    ElevenLabsAPIError,
    ElevenLabsQuotaError,
    generate_speech_elevenlabs,
    generate_speech_with_key_rotation,
)

# ──────────────────────────────────────────────────────────────────────────────
# Group 1: generate_speech_elevenlabs — HTTP layer
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_empty_string():
    res = await generate_speech_elevenlabs("", "api_key", voice_id="voice")
    assert res is None


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_whitespace():
    res = await generate_speech_elevenlabs("   \n ", "api_key", voice_id="voice")
    assert res is None


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"a" * 150

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        res = await generate_speech_elevenlabs("Hello", "key", voice_id="voice")
        assert res == b"a" * 150


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_too_small():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"a" * 50

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        res = await generate_speech_elevenlabs("Hello", "key", voice_id="voice")
        assert res is None


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_401():
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        with pytest.raises(ElevenLabsQuotaError):
            await generate_speech_elevenlabs("Hello", "key", voice_id="voice")


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_403():
    mock_resp = MagicMock()
    mock_resp.status_code = 403

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        with pytest.raises(ElevenLabsQuotaError):
            await generate_speech_elevenlabs("Hello", "key", voice_id="voice")


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_429():
    mock_resp = MagicMock()
    mock_resp.status_code = 429

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        with pytest.raises(ElevenLabsQuotaError):
            await generate_speech_elevenlabs("Hello", "key", voice_id="voice")


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_500():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        with pytest.raises(ElevenLabsAPIError):
            await generate_speech_elevenlabs("Hello", "key", voice_id="voice")


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_previous_text():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"a" * 150

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        await generate_speech_elevenlabs("Hello", "key", voice_id="voice", previous_text="Prev")
        payload = mock_client.post.call_args[1]["json"]
        assert payload["previous_text"] == "Prev"


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_no_previous_text():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"a" * 150

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        await generate_speech_elevenlabs("Hello", "key", voice_id="voice", previous_text=None)
        payload = mock_client.post.call_args[1]["json"]
        assert "previous_text" not in payload


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_apply_text_normalization():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"a" * 150

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        await generate_speech_elevenlabs("Hello", "key", voice_id="voice")
        payload = mock_client.post.call_args[1]["json"]
        assert payload["apply_text_normalization"] == "on"


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_output_format():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"a" * 150

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        await generate_speech_elevenlabs("Hello", "key", voice_id="voice")
        payload = mock_client.post.call_args[1]["json"]
        assert payload["output_format"] == "pcm_24000"


# ──────────────────────────────────────────────────────────────────────────────
# Group 2: generate_speech_with_key_rotation — Atomic Router
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_empty_keys():
    res = await generate_speech_with_key_rotation(["Hello"], [], voice_id="voice")
    assert res is None


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_single_chunk_success():
    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", AsyncMock(return_value=b"audio_bytes")):
        res = await generate_speech_with_key_rotation(["Hello"], ["key1"], voice_id="voice")
        assert res == [b"audio_bytes"]


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_three_chunks_success():
    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", AsyncMock(return_value=b"audio_bytes")):
        res = await generate_speech_with_key_rotation(["C1", "C2", "C3"], ["key1"], voice_id="voice")
        assert res == [b"audio_bytes", b"audio_bytes", b"audio_bytes"]


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_rotates_on_quota():
    call_count = 0
    async def mock_gen(text, api_key, **kwargs):
        nonlocal call_count
        call_count += 1
        if api_key == "key1":
            raise ElevenLabsQuotaError("quota")
        return b"success_bytes"

    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", side_effect=mock_gen):
        res = await generate_speech_with_key_rotation(["Hello"], ["key1", "key2"], voice_id="voice")
        assert res == [b"success_bytes"]
        assert call_count == 2


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_all_keys_exhausted():
    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", AsyncMock(side_effect=ElevenLabsQuotaError("quota"))):
        res = await generate_speech_with_key_rotation(["Hello"], ["key1", "key2"], voice_id="voice")
        assert res is None


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_on_chunk_complete_callbacks():
    completed = []
    async def on_complete(current, total):
        completed.append((current, total))

    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", AsyncMock(return_value=b"audio_bytes")):
        res = await generate_speech_with_key_rotation(
            ["C1", "C2"],
            ["key1"],
            voice_id="voice",
            on_chunk_complete=on_complete
        )
        assert res == [b"audio_bytes", b"audio_bytes"]
        assert completed == [(1, 2), (2, 2)]


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_skips_empty_chunks():
    mock_gen = AsyncMock(return_value=b"audio_bytes")
    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", mock_gen):
        res = await generate_speech_with_key_rotation(["C1", "  ", "", "C2"], ["key1"], voice_id="voice")
        assert res == [b"audio_bytes", b"audio_bytes"]
        assert mock_gen.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# Group 3: fetch_voices — GET /v1/voices
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_voices_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "voices": [
            {"voice_id": "v1", "name": "Charlotte", "category": "premade", "labels": {"accent": "american"}},
            {"voice_id": "v2", "name": "ClonedVoice", "category": "cloned", "labels": {}},
        ]
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        # We access fetch_voices via getattr to allow RED compilation before fetch_voices exists
        fetch_voices_func = getattr(elevenlabs_tts, "fetch_voices")
        res = await fetch_voices_func("api_key")
        assert len(res) == 2
        assert res[0]["id"] == "v1"
        assert res[0]["name"] == "Charlotte"
        assert res[0]["category"] == "premade"
        assert res[1]["id"] == "v2"
        assert res[1]["name"] == "ClonedVoice"
        assert res[1]["category"] == "cloned"


@pytest.mark.asyncio
async def test_fetch_voices_filters_generated():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "voices": [
            {"voice_id": "v1", "name": "Premade", "category": "premade", "labels": {}},
            {"voice_id": "v2", "name": "Generated", "category": "generated", "labels": {}},
        ]
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        fetch_voices_func = getattr(elevenlabs_tts, "fetch_voices")
        res = await fetch_voices_func("api_key")
        assert len(res) == 1
        assert res[0]["id"] == "v1"


@pytest.mark.asyncio
async def test_fetch_voices_401_returns_empty():
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        fetch_voices_func = getattr(elevenlabs_tts, "fetch_voices")
        res = await fetch_voices_func("api_key")
        assert res == []


@pytest.mark.asyncio
async def test_fetch_voices_network_error_returns_empty():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        fetch_voices_func = getattr(elevenlabs_tts, "fetch_voices")
        res = await fetch_voices_func("api_key")
        assert res == []


@pytest.mark.asyncio
async def test_fetch_voices_missing_id_skipped():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "voices": [
            {"voice_id": "v1", "name": "Premade", "category": "premade", "labels": {}},
            {"name": "NoID", "category": "premade", "labels": {}},
        ]
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("app.providers.elevenlabs_tts._get_client", return_value=mock_client):
        fetch_voices_func = getattr(elevenlabs_tts, "fetch_voices")
        res = await fetch_voices_func("api_key")
        assert len(res) == 1
        assert res[0]["id"] == "v1"
