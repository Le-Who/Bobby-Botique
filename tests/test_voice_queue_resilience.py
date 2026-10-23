"""Queue admission and publication ownership with deterministic offline transports."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import voice_engine
from app.utils.background_tasks import TaskManager


@pytest.fixture
def manager(monkeypatch):
    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    owner = TaskManager()
    monkeypatch.setattr(voice_engine, "try_submit_task", owner.try_submit, raising=False)
    instance = voice_engine.VoiceReplyManager()
    return instance, owner


def enqueue(manager, key="one", text="safe text", user_id=1):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        send_voice=AsyncMock(),
        delete_message=AsyncMock(),
    )
    return manager.enqueue(
        bot=bot,
        user_id=user_id,
        chat_id=user_id,
        reply_to_message_id=1,
        response_text=text,
        source_key=key,
        expected_epoch=1,
    )


@pytest.mark.asyncio
async def test_mandatory_task_rejection_is_explicit_and_closes_coroutine():
    owner = TaskManager()
    owner.MAX_TASKS = 0
    ran = []

    async def work():
        ran.append(True)

    assert owner.try_submit(work()) is None
    assert ran == []


@pytest.mark.asyncio
async def test_concurrent_admission_never_waits_on_status_or_starts_two_workers(manager):
    instance, owner = manager
    status_started, status_release = asyncio.Event(), asyncio.Event()
    started, release = [], asyncio.Event()

    async def status(*args, **kwargs):
        status_started.set()
        await status_release.wait()

    async def process(job):
        started.append(job.source_key)
        await release.wait()

    instance._set_status = status
    instance._process_job = process
    try:
        first, second = await asyncio.wait_for(asyncio.gather(enqueue(instance), enqueue(instance, "two")), 0.5)
        await asyncio.wait_for(status_started.wait(), 0.5)
        assert first.queued and second.queued
        assert started == ["one"]
    finally:
        release.set()
        status_release.set()
        await owner.drain(timeout=1)
    assert started == ["one", "two"]


@pytest.mark.asyncio
async def test_rejected_worker_does_not_retain_private_text(manager):
    instance, owner = manager
    owner.MAX_TASKS = 0
    result = await enqueue(instance)
    assert not result.queued and result.reason_code == "capacity"
    assert not instance._queues and not instance._active_jobs


@pytest.mark.asyncio
async def test_bounds_include_active_jobs_and_utf8_bytes(manager):
    instance, owner = manager
    entered, release = asyncio.Event(), asyncio.Event()

    async def process(job):
        entered.set()
        await release.wait()

    instance._process_job = process
    try:
        assert (await enqueue(instance)).queued
        await entered.wait()
        for index in range(2, 9):
            assert (await enqueue(instance, str(index))).queued
        result = await enqueue(instance, "nine")
        assert not result.queued and result.reason_code == "user_capacity"
        oversized = await enqueue(instance, "big", "я" * 32769, user_id=2)
        assert not oversized.queued and oversized.reason_code == "text_too_large"
    finally:
        release.set()
        await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_lease_cancellation_is_job_scoped_and_next_job_runs(manager):
    instance, owner = manager
    first_started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def process(job):
        seen.append(job.source_key)
        if job.source_key == "one":
            first_started.set()
            await release.wait()
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

    instance._process_job = process
    await enqueue(instance)
    await first_started.wait()
    await enqueue(instance, "two")
    release.set()
    await instance.wait_until_idle(1, timeout=1)
    await owner.drain(timeout=1)
    assert seen == ["one", "two"]


@pytest.mark.asyncio
async def test_close_cancels_scrubs_and_rejects_new_work(manager):
    instance, owner = manager
    started = asyncio.Event()
    captured = []

    async def process(job):
        captured.append(job)
        started.set()
        await asyncio.Event().wait()

    instance._process_job = process
    await enqueue(instance)
    await started.wait()
    await enqueue(instance, "two")
    await instance.close()
    result = await enqueue(instance, "three")
    assert not result.queued and result.reason_code == "closed"
    assert captured[0].response_text == ""
    assert not instance._queues and not instance._active_jobs and not instance._worker_tasks
    await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_status_creation_serialized_and_terminal_state_never_regresses(manager):
    instance, owner = manager
    entered, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def send(**kwargs):
        messages.append(kwargs)
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=len(messages))

    bot = SimpleNamespace(send_message=send, edit_message_text=AsyncMock())
    job = voice_engine.VoiceJob("job", 1, 1, 1, "safe", "", None, "key", bot, 1)
    first = asyncio.create_task(instance._set_status(job, status="done", detail="done"))
    await entered.wait()
    second = asyncio.create_task(instance._set_status(job, status="queued", detail="old"))
    release.set()
    await asyncio.gather(first, second)
    assert len(messages) == 1 and job.state.status == "done"
    await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_gemini_request_limit_counts_each_hedged_key(monkeypatch):
    inflight = 0
    maximum = 0
    saturated, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        nonlocal inflight, maximum
        inflight += 1
        maximum = max(maximum, inflight)
        if inflight == 3:
            saturated.set()
        try:
            await release.wait()
            return b"pcm"
        finally:
            inflight -= 1

    async def key(*args, excluded_hashes, **kwargs):
        return {"key_hash": "b" if "a" in excluded_hashes else "a", "api_key": "fake"}

    monkeypatch.setattr("app.providers.tts.generate_speech", generate)
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))
    semaphore = asyncio.Semaphore(3)
    tasks = [
        asyncio.create_task(
            voice_engine._generate_single_chunk_gemini("safe", "Aoede", set(), request_semaphore=semaphore)
        )
        for _ in range(3)
    ]
    try:
        await asyncio.wait_for(saturated.wait(), 1)
        assert maximum == 3
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert maximum == 3 and inflight == 0


@pytest.mark.asyncio
async def test_successful_send_does_not_wait_for_status_deletion(manager, monkeypatch):
    instance, owner = manager
    deleting, release = asyncio.Event(), asyncio.Event()

    async def delete_later(*args):
        deleting.set()
        await release.wait()

    monkeypatch.setattr(voice_engine, "_delete_status_later", delete_later, raising=False)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_message_text=AsyncMock(),
        send_voice=AsyncMock(),
    )
    job = voice_engine.VoiceJob("job", 1, 1, 1, "safe", "", None, "key", bot, 1)
    try:
        await asyncio.wait_for(instance._send_ogg(job, b"ogg"), 0.5)
        await asyncio.wait_for(deleting.wait(), 0.5)
        assert job.state.status == "done"
    finally:
        release.set()
        await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_default_gemini_plan_requires_complete_message_and_reports_progress(manager, monkeypatch):
    from types import MappingProxyType

    from app.process_policies import ResolvedPolicy
    from app.runtime_settings.lifecycle import runtime_settings_scope
    from app.runtime_settings.store import SettingsSnapshot

    instance, owner = manager
    observed = []

    async def resolve(process_id, baseline):
        return ResolvedPolicy(("gemini",)) if process_id == "tts.delivery" else ResolvedPolicy(baseline)

    async def generate(chunks, voice, timeout, **kwargs):
        assert kwargs["require_complete"] is True
        assert kwargs["request_semaphore"] is instance._gemini_sem
        await kwargs["on_chunk_complete"](1, len(chunks))
        observed.append((job.state.provider, job.state.completed_chunks, job.state.total_chunks))
        return [b"pcm"]

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", generate)
    monkeypatch.setattr("app.utils.audio.crossfade_pcm_chunks", lambda parts: b"pcm")
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)), edit_message_text=AsyncMock()
    )
    job = voice_engine.VoiceJob("job", 1, 1, 1, "safe text", "", None, "key", bot, 1)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        assert await instance._pregenerate_audio(job) == b"ogg"
    assert observed == [("gemini", 1, 1)]
    assert job.state.status == "encoding"
    await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_contract_output_failure_never_suspends_a_healthy_key(monkeypatch):
    from app.providers.tts_audio import TTSOutputError

    suspended = []

    async def suspend(*args):
        suspended.append(args)

    async def key(*args, excluded_hashes, **kwargs):
        for value in ("a", "b"):
            if value not in excluded_hashes:
                return {"key_hash": value, "api_key": "safe"}
        return None

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    monkeypatch.setattr("app.providers.tts.generate_speech", AsyncMock(side_effect=TTSOutputError("invalid_audio")))
    assert await voice_engine._generate_single_chunk_gemini("safe", "Aoede", set()) is None
    assert suspended == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["capacity", "user_capacity", "closed", "stale_epoch", "text_too_large"])
async def test_listen_callback_reports_rejected_admission(monkeypatch, reason):
    from app.handlers.cb_ai_actions import tts_reply_callback

    answers = []

    async def answer(text):
        answers.append(text)

    query = SimpleNamespace(
        id="safe",
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(
            text="safe response", reply_markup=None, chat_id=1, message_id=1, chat=SimpleNamespace(id=1)
        ),
        get_bot=lambda: None,
        answer=answer,
    )
    monkeypatch.setattr(
        "app.repos.chats.get_user_chat", AsyncMock(return_value=SimpleNamespace(voice_id="", tts_temperature=None))
    )
    monkeypatch.setattr(
        voice_engine,
        "fire_voice_reply",
        AsyncMock(return_value=voice_engine.VoiceEnqueueResult(False, False, None, 0, reason)),
    )
    await tts_reply_callback(SimpleNamespace(callback_query=query), SimpleNamespace())
    assert len(answers) == 1 and "Генерирую" not in answers[0]
    assert "❌" in answers[0] or "⏳" in answers[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_close_retains_ownership_until_job_cancellation_cleanup_finishes(manager, repeat_cancel):
    instance, owner = manager
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def process(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    instance._process_job = process
    await enqueue(instance)
    await entered.wait()
    closer = asyncio.create_task(instance.close())
    try:
        await cleaning.wait()
        if repeat_cancel:
            closer.cancel()
            await asyncio.sleep(0)
            closer.cancel()
            await asyncio.sleep(0)
            assert not closer.done()
        release.set()
        if repeat_cancel:
            with pytest.raises(asyncio.CancelledError):
                await closer
        else:
            await closer
        assert finished.is_set()
        assert not instance._active_jobs and not instance._worker_tasks
    finally:
        release.set()
        await asyncio.gather(closer, return_exceptions=True)
        await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_close_before_worker_first_instruction_scrubs_captured_input(manager):
    instance, owner = manager
    await enqueue(instance)
    job = instance._queues[1][0]
    await instance.close()
    assert job.response_text == "" and instance._retained_bytes == 0 and instance._retained_jobs == 0
    assert not instance._worker_tasks
    await owner.drain(timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound,value", [("_MAX_JOBS", 3), ("_MAX_RETAINED_BYTES", 30)])
async def test_global_admission_bounds_multiple_users_and_releases_scrubbed_capacity(
    manager, monkeypatch, bound, value
):
    instance, owner = manager
    release = asyncio.Event()
    monkeypatch.setattr(voice_engine, bound, value)

    async def process(job):
        await release.wait()

    instance._process_job = process
    try:
        for user_id in (1, 2, 3):
            assert (await enqueue(instance, user_id=user_id)).queued
        result = await enqueue(instance, user_id=4)
        assert not result.queued and result.reason_code == "capacity"
        await instance.purge_user_jobs(3)
        assert (await enqueue(instance, user_id=4)).queued
    finally:
        release.set()
        await instance.close()
        await owner.drain(timeout=1)
    assert instance._retained_bytes == 0 and instance._retained_jobs == 0


@pytest.mark.asyncio
async def test_absolute_synthesis_deadline_awaits_cleanup_and_never_publishes(manager, monkeypatch):
    instance, owner = manager
    cleaned = asyncio.Event()
    published = []
    captured = []
    monkeypatch.setattr(voice_engine, "_SYNTHESIS_TIMEOUT_S", 0.01)

    async def synthesize(job):
        captured.append(job)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def publish(*args):
        published.append(True)

    instance._pregenerate_audio = synthesize
    instance._send_ogg = publish
    await enqueue(instance)
    await instance.wait_until_idle(1, timeout=1)
    await owner.drain(timeout=1)
    assert cleaned.is_set() and published == []
    assert captured[0].state.status == "failed" and captured[0].response_text == ""


@pytest.mark.asyncio
async def test_restart_reopens_only_after_previous_jobs_finish_cleanup(manager):
    instance, owner = manager
    entered, cleaning, release = (asyncio.Event() for _ in range(3))

    async def process(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    instance._process_job = process
    await enqueue(instance)
    await entered.wait()
    closer = asyncio.create_task(instance.close())
    await cleaning.wait()
    try:
        with pytest.raises(RuntimeError, match="drain"):
            await instance.start()
    finally:
        release.set()
        await closer
    await instance.start()
    instance._process_job = AsyncMock()
    assert (await enqueue(instance, "after-restart")).queued
    await instance.wait_until_idle(1, timeout=1)
    await instance.close()
    await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_slow_status_transport_does_not_delay_synthesis_or_publication(manager):
    instance, owner = manager
    status_started, release, published = (asyncio.Event() for _ in range(3))

    async def send_status(**kwargs):
        status_started.set()
        await release.wait()
        return SimpleNamespace(message_id=10)

    async def send_voice(**kwargs):
        published.set()

    bot = SimpleNamespace(
        send_message=send_status,
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        send_voice=send_voice,
        delete_message=AsyncMock(),
    )
    instance._pregenerate_audio = AsyncMock(return_value=b"ogg")
    try:
        await instance.enqueue(
            bot=bot,
            user_id=1,
            chat_id=1,
            reply_to_message_id=1,
            response_text="safe",
            source_key="key",
            expected_epoch=1,
        )
        await asyncio.wait_for(status_started.wait(), 0.5)
        await asyncio.wait_for(published.wait(), 0.5)
    finally:
        release.set()
        await instance.close()
        await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_concurrent_purge_does_not_cancel_child_cleanup_twice(manager):
    instance, owner = manager
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def process(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    instance._process_job = process
    await enqueue(instance)
    await entered.wait()
    first = asyncio.create_task(instance.purge_user_jobs(1))
    await cleaning.wait()
    second = asyncio.create_task(instance.purge_user_jobs(1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert not first.done() and not second.done()
        release.set()
        await asyncio.gather(first, second)
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await instance.close()
        await owner.drain(timeout=1)


@pytest.mark.asyncio
async def test_contract_failure_abandons_model_without_racing_more_keys(monkeypatch):
    from app.providers.tts_audio import TTSRequestError

    requests = []

    async def key(*args, excluded_hashes, **kwargs):
        for value in ("a", "b", "c", "d"):
            if value not in excluded_hashes:
                return {"key_hash": value, "api_key": value}
        return None

    async def generate(text, key, **kwargs):
        requests.append(key)
        raise TTSRequestError("invalid_request")

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))
    monkeypatch.setattr("app.providers.tts.generate_speech", generate)
    assert await voice_engine._generate_single_chunk_gemini("safe", "Aoede", set()) is None
    assert requests == ["a", "b"]


@pytest.mark.asyncio
async def test_oversized_aggregate_is_incomplete_audio_for_model_fallback(monkeypatch):
    monkeypatch.setattr("app.providers.tts_audio.MAX_TTS_AUDIO_BYTES", 3)
    monkeypatch.setattr(voice_engine, "_generate_single_chunk_gemini", AsyncMock(return_value=b"12"))
    assert await voice_engine._run_gemini_pipeline(["one", "two"], "Aoede", 1, require_complete=True) is None
