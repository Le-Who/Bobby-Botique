# /tests/test_elevenlabs_tts.py
"""Unit tests for the ElevenLabs TTS provider HTTP layer, key rotation router, and voice fetcher."""

import asyncio
import json
import logging
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


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, wait_after_chunks=False):
        self.chunks = chunks
        self.wait_after_chunks = wait_after_chunks
        self.reads = 0
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.wait_after_chunks:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mime", "body"),
    [("application/json", b'{"error":"' + b"x" * 150 + b'"}'), ("audio/pcm", b"x" * 151)],
)
async def test_successful_http_response_must_satisfy_pcm_contract(monkeypatch, mime, body):
    from app.providers.tts_audio import TTSOutputError

    async def handler(request):
        return httpx.Response(200, headers={"content-type": mime}, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        with pytest.raises(TTSOutputError):
            await generate_speech_elevenlabs("Hello", "fake-key", voice_id="voice")


@pytest.mark.asyncio
async def test_audio_read_stops_at_bound_and_closes_stream(monkeypatch):
    from app.providers.tts_audio import TTSOutputError

    stream = TrackedStream([b"x" * 16384, b"y" * 16384, b"never-read"])

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=stream)

    monkeypatch.setattr(elevenlabs_tts, "MAX_TTS_AUDIO_BYTES", 16384, raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        with pytest.raises(TTSOutputError, match="audio_too_large"):
            await generate_speech_elevenlabs("Hello", "fake-key", voice_id="voice")
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", [False, True])
async def test_cancelled_request_finishes_attempt_and_closes_stream(monkeypatch, catalog):
    stream = TrackedStream(wait_after_chunks=True)
    events = []
    monkeypatch.setattr(
        "app.observability.workload_events.emit", lambda event, **fields: events.append((event, fields))
    )

    async def handler(request):
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        coroutine = (
            elevenlabs_tts.fetch_voices("fake-key")
            if catalog
            else generate_speech_elevenlabs("Hello", "fake-key", voice_id="voice")
        )
        task = asyncio.create_task(coroutine)
        await asyncio.wait_for(stream.waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    terminals = [fields for event, fields in events if event == "workload.attempt_finished"]
    assert [row["outcome"] for row in terminals] == ["cancelled"]
    assert stream.closed


@pytest.mark.asyncio
async def test_absolute_deadline_covers_reading_entire_body(monkeypatch):
    stream = TrackedStream([b"\x01\x00" * 100], wait_after_chunks=True)
    events = []
    monkeypatch.setattr(
        "app.observability.workload_events.emit", lambda event, **fields: events.append((event, fields))
    )

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        with pytest.raises(TimeoutError):
            await generate_speech_elevenlabs("Hello", "fake-key", voice_id="voice", timeout=0.01)
    terminals = [fields for event, fields in events if event == "workload.attempt_finished"]
    assert len(terminals) == 1
    assert terminals[0]["reason_code"] == "timeout"
    assert stream.closed


@pytest.mark.asyncio
async def test_stitching_keeps_only_nearest_bounded_context(monkeypatch):
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x01\x00" * 100)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        await generate_speech_elevenlabs(
            "Hello", "fake-key", voice_id="voice", previous_text="a" * 1000 + "tail", next_text="head" + "b" * 1000
        )
    assert sent[0]["previous_text"] == "a" * 496 + "tail"
    assert sent[0]["next_text"] == "head" + "b" * 496


@pytest.mark.asyncio
async def test_close_client_closes_and_clears_owned_pool(monkeypatch):
    client = httpx.AsyncClient()
    monkeypatch.setattr(elevenlabs_tts, "_http_client", client)
    await elevenlabs_tts.close_elevenlabs_client()
    assert client.is_closed
    assert elevenlabs_tts._http_client is None
    await elevenlabs_tts.close_elevenlabs_client()


@pytest.mark.asyncio
async def test_complete_message_has_aggregate_audio_bound(monkeypatch):
    async def handler(request):
        return httpx.Response(200, content=b"\x01\x00" * 100)

    monkeypatch.setattr(elevenlabs_tts, "MAX_TTS_AUDIO_BYTES", 300)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        assert await generate_speech_with_key_rotation(["one", "two"], ["fake-key"], voice_id="voice") is None


@pytest.mark.asyncio
async def test_request_deadline_and_terminal_cover_configuration(monkeypatch):
    events = []
    monkeypatch.setattr(
        "app.observability.workload_events.emit", lambda event, **fields: events.append((event, fields))
    )

    async def stuck_config(provider):
        await asyncio.Event().wait()

    monkeypatch.setattr(elevenlabs_tts, "get_config", stuck_config)
    with pytest.raises(TimeoutError):
        await generate_speech_elevenlabs("Hello", "fake-key", voice_id="voice", timeout=0.01)
    terminals = [fields for event, fields in events if event == "workload.attempt_finished"]
    assert len(terminals) == 1
    assert terminals[0]["reason_code"] == "timeout"


@pytest.mark.asyncio
async def test_catalog_cancel_during_error_response_close_has_one_terminal(monkeypatch):
    closing = asyncio.Event()
    events = []
    monkeypatch.setattr(
        "app.observability.workload_events.emit", lambda event, **fields: events.append((event, fields))
    )

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"unread error body"

        async def aclose(self):
            closing.set()
            await asyncio.Event().wait()

    async def handler(request):
        return httpx.Response(401, stream=SlowClose())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        task = asyncio.create_task(elevenlabs_tts.fetch_voices("key"))
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    terminals = [fields for event, fields in events if event == "workload.attempt_finished"]
    assert [row["outcome"] for row in terminals] == ["cancelled"]


# ──────────────────────────────────────────────────────────────────────────────
# Group 1: HTTP requests exercise real httpx framing and response objects.


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   \n "])
async def test_generate_speech_elevenlabs_empty_input(text):
    assert await generate_speech_elevenlabs(text, "api_key", voice_id="voice") is None


@pytest.mark.asyncio
async def test_generate_speech_elevenlabs_success_and_request_payload(monkeypatch):
    captured = []

    async def handler(request):
        captured.append(request)
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x01\x00" * 75)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        assert (
            await generate_speech_elevenlabs("Hello", "key", voice_id="voice", previous_text="Prev") == b"\x01\x00" * 75
        )
    request = captured[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/text-to-speech/voice/stream"
    assert dict(request.url.params) == {"output_format": "pcm_24000"}
    assert payload["text"] == "Hello"
    assert payload["previous_text"] == "Prev"
    assert "next_text" not in payload
    assert payload["apply_text_normalization"] == "on"
    assert "output_format" not in payload
    assert request.headers["xi-api-key"] == "key"
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_generate_speech_http_failure_closes_without_reading_private_body(monkeypatch, caplog, status):
    stream = TrackedStream([b"private-upstream-body" * 10000])

    async def handler(request):
        return httpx.Response(status, stream=stream)

    error_type = ElevenLabsQuotaError if status in (401, 403, 429) else ElevenLabsAPIError
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        with caplog.at_level(logging.INFO), pytest.raises(error_type) as error:
            await generate_speech_elevenlabs("Hello", "key", voice_id="voice")
    assert "private-upstream-body" not in caplog.text
    assert "private-upstream-body" not in str(error.value)
    assert stream.reads == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_invalid_request_does_not_retry_unchanged_payload_for_other_keys(monkeypatch):
    called = []

    async def handler(request):
        called.append(request.headers["xi-api-key"])
        return httpx.Response(422, content=b"invalid voice or settings")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        assert await generate_speech_with_key_rotation(["Hello"], ["key-one", "key-two"], voice_id="voice") is None
    assert called == ["key-one"]


@pytest.mark.asyncio
async def test_late_chunk_failure_discards_preceding_successful_audio(monkeypatch):
    async def handler(request):
        if json.loads(request.content)["text"] == "first":
            return httpx.Response(200, content=b"\x01\x00" * 75)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        assert await generate_speech_with_key_rotation(["first", "second"], ["key"], voice_id="voice") is None


# # Group 2: generate_speech_with_key_rotation — Atomic Router
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
    with patch(
        "app.providers.elevenlabs_tts.generate_speech_elevenlabs", AsyncMock(side_effect=ElevenLabsQuotaError("quota"))
    ):
        res = await generate_speech_with_key_rotation(["Hello"], ["key1", "key2"], voice_id="voice")
        assert res is None


@pytest.mark.asyncio
async def test_generate_speech_with_key_rotation_on_chunk_complete_callbacks():
    completed = []

    async def on_complete(current, total):
        completed.append((current, total))

    with patch("app.providers.elevenlabs_tts.generate_speech_elevenlabs", AsyncMock(return_value=b"audio_bytes")):
        res = await generate_speech_with_key_rotation(
            ["C1", "C2"], ["key1"], voice_id="voice", on_chunk_complete=on_complete
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
# Group 3: bounded voice catalog reads and parsing.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "want"),
    [
        (
            {
                "voices": [
                    {"voice_id": "v1", "name": "Charlotte", "category": "premade", "labels": {"accent": "american"}},
                    {"voice_id": "v2", "name": "ClonedVoice", "category": "cloned", "labels": {}},
                ]
            },
            ["v1", "v2"],
        ),
        (
            {
                "voices": [
                    {"voice_id": "v1", "name": "Premade", "category": "premade"},
                    {"voice_id": "v2", "name": "Generated", "category": "generated"},
                    {"name": "MissingID", "category": "premade"},
                ]
            },
            ["v1"],
        ),
        (["unexpected"], []),
        ({"voices": "unexpected"}, []),
    ],
)
async def test_fetch_voices_parses_only_valid_catalog_rows(monkeypatch, payload, want):
    async def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/voices"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        result = await elevenlabs_tts.fetch_voices("key")
    assert [row["id"] for row in result] == want


@pytest.mark.asyncio
async def test_fetch_voices_transport_error_is_sanitized(monkeypatch, caplog):
    async def handler(request):
        raise httpx.ConnectError("private-transport-detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert await elevenlabs_tts.fetch_voices("key") == []
    assert "private-transport-detail" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_voices_http_failure_returns_empty_without_reading_body(monkeypatch):
    stream = TrackedStream([b"do-not-read"])

    async def handler(request):
        return httpx.Response(401, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.invalid") as client:
        monkeypatch.setattr(elevenlabs_tts, "_get_client", lambda: client)
        assert await elevenlabs_tts.fetch_voices("key") == []
    assert stream.reads == 0
    assert stream.closed
