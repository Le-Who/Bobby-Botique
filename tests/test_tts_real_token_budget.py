"""Real model token admission, independent of transcript UTF-8 size."""

import asyncio
import base64
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.genai import types

PCM = b"\x01\x00" * 120


@pytest.fixture
def isolated(monkeypatch):
    from app.providers import tts
    from app.runtime_settings import lifecycle, store

    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=store.SettingsSnapshot(1, MappingProxyType({}))))
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(
        tts,
        "get_config",
        AsyncMock(
            return_value={
                "default_voice": "Aoede",
                "voices": [{"id": "Aoede", "prompt": "Quiet clear delivery."}],
                "temperature": 0.2,
                "max_output_tokens": 16384,
            }
        ),
    )
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key", AsyncMock(return_value={"key_hash": "offline", "api_key": "offline"})
    )
    suspend = AsyncMock()
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    return tts, reserve, suspend


def completed_audio():
    return {
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
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("max_output_tokens", [1024, 16384])
async def test_russian_source_over_old_byte_caps_is_one_actual_tts_request(isolated, monkeypatch, max_output_tokens):
    tts, reserve, _ = isolated
    tts.get_config.return_value["max_output_tokens"] = max_output_tokens
    source = "Содержательное русское предложение. " * 140
    assert len(source.encode("utf-8")) > 8192
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        return httpx.Response(
            200, json={"totalTokens": 1042} if request.url.path.endswith(":countTokens") else completed_audio()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            assert await tts.generate_speech(source, "offline", language_code="ru-RU") == PCM
        finally:
            await client.aio.aclose()
            client.close()
    assert [path for path, _ in requests] == [
        "/v1beta/models/gemini-3.8-flash-lite-tts:countTokens",
        "/v1beta/interactions",
    ]
    counted = requests[0][1]["contents"][0]["parts"]
    assert source.strip() in [part["text"] for part in counted]
    assert any("Quiet clear delivery." in part["text"] for part in counted)
    assert any("Aoede" in part["text"] for part in counted)
    assert any("ru-RU" in part["text"] for part in counted)
    generated = requests[1][1]
    assert generated["input"][0]["content"][0]["text"] == source.strip()
    assert generated["generation_config"]["max_output_tokens"] == max_output_tokens
    assert reserve.await_count == 1


@pytest.mark.asyncio
async def test_counted_oversize_uses_three_fitting_requests_without_binary_four(isolated, monkeypatch):
    from app import voice_engine

    tts, reserve, suspend = isolated
    source = "x " * 12000
    counted, generated = [], []

    def respond(request):
        payload = json.loads(request.content)
        if request.url.path.endswith(":countTokens"):
            transcript = payload["contents"][0]["parts"][-1]["text"]
            tokens = len(transcript.split()) * 2 + 100
            counted.append((transcript, tokens))
            return httpx.Response(200, json={"totalTokens": tokens})
        transcript = payload["input"][0]["content"][0]["text"]
        assert len(transcript.split()) * 2 + 100 <= 8192
        generated.append(transcript)
        return httpx.Response(200, json=completed_audio())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            prepared = await tts.prepare_speech_settings()
            assert await voice_engine._run_gemini_pipeline([source], "Aoede", 5, speech_settings=prepared) == [PCM] * 3
        finally:
            await client.aio.aclose()
            client.close()
    assert "".join(generated) == source
    assert reserve.await_count == 3
    assert len(counted) <= 30
    suspend.assert_not_awaited()


@pytest.mark.asyncio
async def test_counter_unavailable_lets_real_tts_validate_whole_without_key_penalty(isolated, monkeypatch):
    tts, reserve, suspend = isolated
    source = "Русский текст остаётся целиком. " * 140
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return (
            httpx.Response(503, json={"error": {"message": "Counter temporarily unavailable"}})
            if request.url.path.endswith(":countTokens")
            else httpx.Response(200, json=completed_audio())
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(
            api_key="offline",
            http_options=types.HttpOptions(
                httpx_async_client=transport, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        monkeypatch.setattr(tts, "get_cached_genai_client", lambda _: client)
        try:
            assert await tts.generate_speech(source, "offline") == PCM
        finally:
            await client.aio.aclose()
            client.close()
    assert len(paths) == 2
    assert reserve.await_count == 1
    suspend.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_reuse_requires_matching_transcript_direction_voice_and_model(isolated, monkeypatch):
    from app.providers.tts_tokens import SpeechTokenCache, count_speech_tokens

    tts, _, _ = isolated
    calls = []

    async def count(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(total_tokens=100)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count)))
    prepared = await tts.prepare_speech_settings(language_code="ru-RU")
    cache = SpeechTokenCache()
    receipt = await count_speech_tokens("source", "offline", prepared, client=client, cache=cache)
    reused = SpeechTokenCache((receipt,))
    assert await count_speech_tokens("source", "second-key", prepared, client=client, cache=reused) == receipt
    for transcript, settings in [
        ("changed", prepared),
        ("source", replace(prepared, direction="Other style")),
        ("source", replace(prepared, voice="Kore")),
        ("source", replace(prepared, model_name="gemini-3.8-flash-tts")),
        ("source", replace(prepared, language_code="en-US")),
    ]:
        await count_speech_tokens(transcript, "offline", settings, client=client, cache=reused)
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_merge_admission_counts_first_selected_model_and_reuses_worker_receipt(isolated, monkeypatch):
    from app import voice_engine
    from app.process_policies import ResolvedPolicy

    tts, reserve, _ = isolated
    source = "A complete merged source."
    count = AsyncMock(return_value=SimpleNamespace(total_tokens=100))
    generate = AsyncMock(
        return_value=SimpleNamespace(
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
    )
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(count_tokens=count), interactions=SimpleNamespace(create=generate)
            )
        ),
    )

    async def resolve(process_id, baseline):
        return (
            ResolvedPolicy(("gemini",))
            if process_id == "tts.delivery"
            else ResolvedPolicy(("gemini-3.8-flash-tts",), "sequential", True, 1)
        )

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    admission = await voice_engine.plan_voice_source_merge(source, voice="Aoede")
    assert admission.allowed
    assert count.call_args.kwargs["model"] == "gemini-3.8-flash-tts"
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    manager._set_status = AsyncMock()
    job = SimpleNamespace(
        response_text=source,
        voice="Aoede",
        tts_temperature=None,
        text_is_clean=True,
        token_receipts=admission.token_receipts,
    )
    assert await manager._pregenerate_audio(job) == b"ogg"
    assert count.await_count == 1
    assert generate.await_count == 1
    assert reserve.await_count == 1


@pytest.mark.asyncio
async def test_merge_admission_unknown_counter_is_false_and_does_not_generate(isolated, monkeypatch):
    from app import voice_engine
    from app.process_policies import ResolvedPolicy

    tts, reserve, suspend = isolated
    count = AsyncMock(side_effect=RuntimeError("unsupported count endpoint"))
    generate = AsyncMock()
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(count_tokens=count), interactions=SimpleNamespace(create=generate)
            )
        ),
    )
    monkeypatch.setattr(
        voice_engine,
        "resolve_process",
        AsyncMock(
            side_effect=lambda process_id, baseline: (
                ResolvedPolicy(("gemini",)) if process_id == "tts.delivery" else ResolvedPolicy(baseline)
            )
        ),
    )
    assert not (await voice_engine.plan_voice_source_merge("Merge source", voice="Aoede")).allowed
    generate.assert_not_awaited()
    reserve.assert_not_awaited()
    suspend.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "source_length", "allowed"),
    [("eleven_v3", 5001, False), ("eleven_flash_v2_5", 10001, True), ("unknown", 20, False)],
)
async def test_merge_admission_honors_first_elevenlabs_model_limit(
    isolated, monkeypatch, model, source_length, allowed
):
    from app import voice_engine
    from app.config import settings
    from app.process_policies import ResolvedPolicy

    monkeypatch.setattr(settings, "ELEVENLABS_API_KEYS", ["offline"])

    async def resolve(process_id, baseline):
        return (
            ResolvedPolicy(("elevenlabs", "gemini"))
            if process_id == "tts.delivery"
            else ResolvedPolicy((model,), "sequential", True, 1)
        )

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    admission = await voice_engine.plan_voice_source_merge("x" * source_length)
    assert admission.allowed is allowed
    assert admission.token_receipts == ()


@pytest.mark.asyncio
async def test_counter_deadline_falls_back_without_spending_whole_generation_deadline(isolated, monkeypatch):
    tts, reserve, suspend = isolated

    async def hanging_counter(**kwargs):
        await asyncio.Event().wait()

    generate = AsyncMock(
        return_value=SimpleNamespace(
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
    )
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(count_tokens=hanging_counter), interactions=SimpleNamespace(create=generate)
            )
        ),
    )
    assert await tts.generate_speech("Whole source.", "offline", timeout=0.2) == PCM
    assert reserve.await_count == 1
    assert generate.await_count == 1
    suspend.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["你好世界。再见朋友。", "Начало.  Ещё слово!\n\nCafé — 👨‍👩‍👧‍👦 рядом.   Last words."]
)
async def test_counted_splitting_preserves_unicode_lexical_units_and_every_separator(isolated, source):
    from app.providers.tts_tokens import SpeechTokenCache, plan_speech_chunks

    tts, _, _ = isolated
    counted = {}

    async def count(**kwargs):
        transcript = kwargs["contents"].parts[-1].text
        tokens = len(transcript) * 1000 + 100
        counted[transcript] = tokens
        return SimpleNamespace(total_tokens=tokens)

    prepared = await tts.prepare_speech_settings()
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count)))
    pieces = await plan_speech_chunks(source, "offline", prepared, client=client, cache=SpeechTokenCache())
    assert "".join(pieces) == source
    assert all(counted[piece] <= 8192 for piece in pieces)
    if "👨" in source:
        assert any("👨‍👩‍👧‍👦" in piece for piece in pieces)


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
async def test_normal_model_deadline_expands_only_for_proven_pieces(isolated, monkeypatch, split):
    from app import voice_engine
    from app.process_policies import ResolvedPolicy
    from app.providers.tts_tokens import SpeechInputSplitRequired

    source = "First. Second. Third."
    deadlines = []
    original_timeout = asyncio.timeout

    def timeout(delay):
        scope = original_timeout(delay)
        deadlines.append((delay, scope))
        return scope

    async def synthesize(transcript, *args, **kwargs):
        if split and transcript == source:
            raise SpeechInputSplitRequired(["First. ", "Second. ", "Third."])
        return PCM

    async def resolve(process_id, baseline):
        return (
            ResolvedPolicy(("gemini",))
            if process_id == "tts.delivery"
            else ResolvedPolicy((baseline[0],), "sequential", True, 1)
        )

    monkeypatch.setattr(voice_engine, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": timeout}))
    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    manager._set_status = AsyncMock()
    job = SimpleNamespace(response_text=source, voice="Aoede", tts_temperature=None)
    start = asyncio.get_running_loop().time()
    assert await manager._pregenerate_audio(job) == b"ogg"
    delay, scope = deadlines[0]
    expected = (len(source) / 40 + 40) * 2
    assert delay == expected
    assert scope.when() - start == pytest.approx(expected * (3 if split else 1), abs=0.1)


@pytest.mark.asyncio
async def test_key_retry_reuses_same_material_count(isolated, monkeypatch):
    from google.genai.errors import ClientError

    from app import voice_engine

    tts, reserve, suspend = isolated
    count = AsyncMock(return_value=SimpleNamespace(total_tokens=100))
    first = True

    async def generate(**kwargs):
        nonlocal first
        if first:
            first = False
            raise ClientError(429, {"error": {"message": "GenerateRequestsPerMinute", "status": "RESOURCE_EXHAUSTED"}})
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

    async def key(*args, excluded_hashes, **kwargs):
        for candidate in ("first-key", "second-key"):
            if candidate not in excluded_hashes:
                return {"key_hash": candidate, "api_key": candidate}
        return None

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(count_tokens=count), interactions=SimpleNamespace(create=generate)
            )
        ),
    )
    prepared = await tts.prepare_speech_settings()
    assert await voice_engine._run_gemini_pipeline(["Whole source."], "Aoede", 1, speech_settings=prepared) == [PCM]
    assert count.await_count == 1
    assert reserve.await_count == 2
    assert suspend.call_args.args == ("first-key", "gemini-3.8-flash-lite-tts", "rate_limit", "ClientError")


@pytest.mark.asyncio
async def test_count_cancellation_awaits_cleanup_before_releasing_slot_without_generation(isolated, monkeypatch):
    from app import voice_engine

    tts, reserve, suspend = isolated
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def count(**kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    generate = AsyncMock()
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(count_tokens=count), interactions=SimpleNamespace(create=generate)
            )
        ),
    )
    semaphore = asyncio.Semaphore(1)
    task = asyncio.create_task(
        voice_engine._generate_single_chunk_gemini("Whole source.", "Aoede", set(), request_semaphore=semaphore)
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    await asyncio.wait_for(semaphore.acquire(), 1)
    semaphore.release()
    generate.assert_not_awaited()
    reserve.assert_not_awaited()
    suspend.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_transports_receipts_and_scrubs_them_after_private_job(isolated, monkeypatch):
    from contextlib import asynccontextmanager

    from app import voice_engine
    from app.providers.tts_tokens import SpeechTokenReceipt

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    manager = voice_engine.VoiceReplyManager()
    monkeypatch.setattr(voice_engine, "_voice_reply_manager", manager)
    receipt = SpeechTokenReceipt("gemini-3.8-flash-lite-tts", "source-hash", "metadata-hash", 100)
    captured = []
    jobs = []

    async def process(job):
        jobs.append(job)
        captured.append(job.token_receipts)

    manager._process_job = process
    try:
        result = await voice_engine.fire_voice_reply(
            bot=SimpleNamespace(),
            user_id=1,
            chat_id=1,
            reply_to_message_id=1,
            response_text="Whole source.",
            source_key="source",
            expected_epoch=1,
            token_receipts=(receipt,),
        )
        assert result.queued
        await manager.wait_until_idle(1)
    finally:
        await manager.close()
    assert captured == [(receipt,)]
    assert jobs[0].token_receipts == ()
    assert jobs[0].response_text == ""


@pytest.mark.asyncio
async def test_raw_source_memory_guard_precedes_cleanup_and_sdk(isolated, monkeypatch):
    from app.providers.tts_audio import TTSRequestError

    tts, reserve, _ = isolated
    client = AsyncMock()
    monkeypatch.setattr(tts, "get_cached_genai_client", client)
    with pytest.raises(TTSRequestError, match="input_too_large"):
        await tts.generate_speech("```" + "x" * 65537 + "```", "offline")
    client.assert_not_called()
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_input_plan_skips_silent_separator_without_losing_audio(isolated, monkeypatch):
    from app import voice_engine
    from app.providers.tts_tokens import SpeechInputSplitRequired

    source = "First. " + " " * 400 + "Last."
    requested = []

    async def generate(transcript, *args, **kwargs):
        requested.append(transcript)
        if transcript == source:
            raise SpeechInputSplitRequired(["First. ", " " * 400, "Last."])
        return PCM if transcript.strip() else None

    monkeypatch.setattr("app.providers.tts.generate_speech", generate)
    assert await voice_engine._run_gemini_pipeline([source], "Aoede", 1) == [PCM, PCM]
    assert requested == [source, "First. ", "Last."]


@pytest.mark.asyncio
async def test_fallback_with_fewer_proven_pieces_never_shortens_admitted_deadlines(isolated, monkeypatch):
    from app import voice_engine
    from app.process_policies import ResolvedPolicy

    source = "One complete source."
    scopes, observed = [], []
    original_timeout = asyncio.timeout

    def timeout(delay):
        scope = original_timeout(delay)
        scopes.append(scope)
        return scope

    async def resolve(process_id, baseline):
        return (
            ResolvedPolicy(("gemini",))
            if process_id == "tts.delivery"
            else ResolvedPolicy(("gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts"), "sequential", True, 1)
        )

    async def pipeline(chunks, voice, timeout, **kwargs):
        first_model = kwargs["model_name"] == "gemini-3.8-flash-lite-tts"
        await kwargs["on_chunk_complete"](0, 5 if first_model else 2)
        observed.append((scopes[-1].when(), job.synthesis_timeout.when()))
        return None if first_model else [PCM]

    monkeypatch.setattr(voice_engine, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": timeout}))
    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", pipeline)
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    manager._set_status = AsyncMock()
    job = SimpleNamespace(
        response_text=source,
        voice="Aoede",
        tts_temperature=None,
        state=SimpleNamespace(started_at=asyncio.get_running_loop().time()),
    )
    async with asyncio.timeout(300) as job.synthesis_timeout:
        assert await manager._pregenerate_audio(job) == b"ogg"
    assert len(observed) == 2
    assert observed[1][0] >= observed[0][0]
    assert observed[1][1] >= observed[0][1]
