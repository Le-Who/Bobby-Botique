"""Voice UI actions bind actor/message/generation and admit a single owned task."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import cb_voice
from app.handlers.msg_voice import _show_confirmation_ui


@pytest.fixture
def voice_case(monkeypatch):
    lock = asyncio.Lock()
    tasks = set()
    pending = {
        "user_id": 101,
        "placeholder_id": 10,
        "memory_epoch": 52,
        "lang": "ru",
        "transcript": "Synthetic transcript",
        "voice_bytes": b"synthetic",
        "file_unique_id": "synthetic-audio",
    }
    other = dict(pending, placeholder_id=20, transcript="Other transcript")
    context = SimpleNamespace(user_data={"voice_pending_10": pending, "voice_pending_20": other})
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        message_id=10,
        chat=SimpleNamespace(id=101),
        reply_text=AsyncMock(return_value=placeholder),
        edit_text=AsyncMock(),
    )
    query = SimpleNamespace(
        data="voice:confirm",
        id="synthetic-callback",
        from_user=SimpleNamespace(id=101),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    chat = SimpleNamespace(memory_epoch=52, history=[], ltm_enabled=False, private_data_blocked=False)
    dispatch = AsyncMock()
    asr = AsyncMock(return_value=("New transcript", "conversational", None))
    current = AsyncMock(return_value=True)
    lease_events = []

    @asynccontextmanager
    async def lease(uid, epoch, *, purpose, require_ltm):
        assert (uid, epoch, require_ltm) == (101, 52, False)
        lease_events.append("enter")
        try:
            yield True
        finally:
            lease_events.append("exit")

    monkeypatch.setattr(cb_voice.state, "get_user_lock", lambda uid: lock)
    monkeypatch.setattr(cb_voice, "_background_tasks", tasks)
    monkeypatch.setattr(cb_voice, "_voice_operations", {})
    monkeypatch.setattr(cb_voice, "_HEAVY_CALLBACK_SEMAPHORE", asyncio.Semaphore(1))
    monkeypatch.setattr(cb_voice, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.handlers.ai_chat._handle_regular_chat", dispatch)
    monkeypatch.setattr("app.handlers.ai_search._handle_research_agent", dispatch)
    monkeypatch.setattr("app.handlers.msg_voice._show_transcript_only", dispatch)
    monkeypatch.setattr("app.handlers.msg_voice._show_confirmation_ui", AsyncMock())
    monkeypatch.setattr("app.utils.multimodal_processor.transcribe_voice", asr)
    monkeypatch.setattr(
        "app.voice_intent.detect_tts_intent", AsyncMock(return_value=SimpleNamespace(explicit_tts=False))
    )
    monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", current)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    return SimpleNamespace(
        lock=lock,
        tasks=tasks,
        pending=pending,
        other=other,
        query=query,
        message=message,
        context=context,
        update=SimpleNamespace(callback_query=query),
        dispatch=dispatch,
        asr=asr,
        current=current,
        lease_events=lease_events,
        chat=chat,
    )


async def run_tasks(case):
    if case.tasks:
        await asyncio.gather(*tuple(case.tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search", "transcribe_only", "retranscribe_flash"])
async def test_wrong_message_never_dispatches_another_pending_voice(voice_case, action):
    voice_case.query.data = f"voice:{action}"
    voice_case.message.message_id = 99
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await run_tasks(voice_case)
    voice_case.dispatch.assert_not_awaited()
    voice_case.asr.assert_not_awaited()
    assert set(voice_case.context.user_data) == {"voice_pending_10", "voice_pending_20"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search", "retranscribe_flash"])
async def test_busy_voice_preserves_pending_until_admission(voice_case, action):
    voice_case.query.data = f"voice:{action}"
    await voice_case.lock.acquire()
    try:
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
    finally:
        voice_case.lock.release()
    assert voice_case.context.user_data["voice_pending_10"] is voice_case.pending
    assert voice_case.tasks == set()
    voice_case.dispatch.assert_not_awaited()
    voice_case.asr.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search", "transcribe_only"])
async def test_confirm_replay_dispatches_once_and_preserves_other_message(voice_case, action):
    voice_case.query.data = f"voice:{action}"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await run_tasks(voice_case)
    assert voice_case.dispatch.await_count == 1
    assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
    assert voice_case.lock.locked() is False


@pytest.mark.asyncio
async def test_retranscribe_replay_admits_only_one_provider_call(voice_case):
    voice_case.query.data = "voice:retranscribe_flash"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await run_tasks(voice_case)
    voice_case.asr.assert_awaited_once_with(b"synthetic", model="gemini-3.5-flash")
    assert voice_case.pending["transcript"] == "New transcript"
    assert voice_case.lock.locked() is False


@pytest.mark.asyncio
async def test_cancel_during_retranscription_never_restores_pending(voice_case):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    voice_case.asr.side_effect = suspended
    voice_case.query.data = "voice:retranscribe_flash"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
        assert cleaned.is_set()
        assert "voice_pending_10" not in voice_case.context.user_data
        assert voice_case.context.user_data["voice_pending_20"] is voice_case.other
        assert voice_case.lock.locked() is False
        assert voice_case.lease_events == ["enter", "exit"]
    finally:
        tasks = tuple(voice_case.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search", "transcribe_only", "retranscribe_flash"])
async def test_stale_generation_never_reintroduces_voice_context(voice_case, action):
    voice_case.current.return_value = False
    voice_case.query.data = f"voice:{action}"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await run_tasks(voice_case)
    voice_case.dispatch.assert_not_awaited()
    voice_case.asr.assert_not_awaited()
    assert voice_case.chat.history == []
    assert "voice_pending_10" not in voice_case.context.user_data


@pytest.mark.asyncio
async def test_access_epoch_revoked_during_asr_does_not_restore_transcript(voice_case):
    started = asyncio.Event()
    release = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        await release.wait()
        return "Must not be restored", "conversational", None

    voice_case.asr.side_effect = suspended
    voice_case.query.data = "voice:retranscribe_flash"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.current.return_value = False
        release.set()
        await run_tasks(voice_case)
    finally:
        release.set()
        tasks = tuple(voice_case.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert "voice_pending_10" not in voice_case.context.user_data
    assert voice_case.pending["transcript"] == "Synthetic transcript"
    assert voice_case.lease_events == ["enter", "exit"]
    assert voice_case.lock.locked() is False


@pytest.mark.asyncio
async def test_missing_actor_binding_rejects_voice_pending(voice_case):
    voice_case.pending["user_id"] = 202
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await run_tasks(voice_case)
    voice_case.dispatch.assert_not_awaited()
    assert voice_case.context.user_data["voice_pending_10"] is voice_case.pending


@pytest.mark.asyncio
async def test_admission_owns_lock_while_placeholder_creation_is_suspended(voice_case):
    started = asyncio.Event()
    release = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(edit_text=AsyncMock())

    voice_case.message.reply_text.side_effect = suspended
    first = asyncio.create_task(cb_voice.voice_callback(voice_case.update, voice_case.context))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert voice_case.lock.locked()
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
        assert voice_case.message.reply_text.await_count == 1
        release.set()
        await first
        await run_tasks(voice_case)
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert voice_case.dispatch.await_count == 1
    assert voice_case.lock.locked() is False


@pytest.mark.asyncio
async def test_ingress_pending_keeps_exact_pre_asr_epoch(monkeypatch, voice_case):
    from app.handlers import msg_voice

    async def asr(*args, **kwargs):
        voice_case.chat.memory_epoch = 99
        return "Synthetic transcript", "conversational", None

    monkeypatch.setattr(msg_voice, "get_user_chat", AsyncMock(return_value=voice_case.chat))
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", AsyncMock(return_value=52))
    monkeypatch.setattr(msg_voice, "get_file_bytes", AsyncMock(return_value=b"synthetic"))
    monkeypatch.setattr(msg_voice, "_detect_show_and_tell", AsyncMock(return_value=None))
    monkeypatch.setattr(msg_voice, "_should_auto_route", lambda transcript: False)
    monkeypatch.setattr("app.intent_router.try_direct_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(msg_voice, "_show_confirmation_ui", _show_confirmation_ui)
    voice_case.asr.side_effect = asr
    voice = SimpleNamespace(get_file=AsyncMock(return_value=object()), file_unique_id="synthetic-audio", duration=1)
    voice_case.context.bot = object()
    await msg_voice._process_voice_pipeline(voice_case.message, voice_case.update, voice_case.context, 101, voice, "ru")
    assert voice_case.context.user_data["voice_pending_10"]["memory_epoch"] == 52


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search"])
async def test_cancel_while_callback_prepares_ui_prevents_dispatch(voice_case, action):
    started = asyncio.Event()
    release = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(edit_text=AsyncMock())

    voice_case.query.data = f"voice:{action}"
    voice_case.message.reply_text.side_effect = suspended
    first = asyncio.create_task(cb_voice.voice_callback(voice_case.update, voice_case.context))
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
        assert first.done()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await run_tasks(voice_case)
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    voice_case.dispatch.assert_not_awaited()
    assert voice_case.lock.locked() is False
    assert "voice_pending_10" not in voice_case.context.user_data


@pytest.mark.asyncio
async def test_preparation_failure_releases_lock_and_preserves_pending(voice_case):
    voice_case.message.reply_text.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
    assert voice_case.lock.locked() is False
    assert voice_case.context.user_data["voice_pending_10"] is voice_case.pending
    assert voice_case.tasks == set()


@pytest.mark.asyncio
async def test_cancelling_confirm_task_releases_owned_lock(voice_case):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    voice_case.dispatch.side_effect = suspended
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await asyncio.wait_for(started.wait(), 1)
    task = voice_case.pending["_task"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert voice_case.lock.locked() is False
    assert "voice_pending_10" not in voice_case.context.user_data
    assert voice_case.tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "exception"])
async def test_failed_retranscription_releases_task_and_preserves_retry(voice_case, failure):
    if failure == "empty":
        voice_case.asr.return_value = (None, "conversational", None)
    else:
        voice_case.asr.side_effect = RuntimeError("ASR unavailable")
    voice_case.query.data = "voice:retranscribe_flash"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    await run_tasks(voice_case)
    assert voice_case.lock.locked() is False
    assert voice_case.tasks == set()
    assert voice_case.context.user_data["voice_pending_10"] is voice_case.pending
    assert voice_case.pending["transcript"] == "Synthetic transcript"
    assert "_task" not in voice_case.pending
    assert voice_case.lease_events == ["enter", "exit"]


@pytest.mark.asyncio
async def test_epoch_revoked_while_deep_search_waits_does_not_append_history(monkeypatch, voice_case):
    waiting = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def admission():
        waiting.set()
        await release.wait()
        yield

    monkeypatch.setattr(cb_voice, "_HEAVY_CALLBACK_SEMAPHORE", admission())
    voice_case.query.data = "voice:deep_search"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        voice_case.current.return_value = False
        release.set()
        await run_tasks(voice_case)
    finally:
        release.set()
        tasks = tuple(voice_case.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert voice_case.chat.history == []
    voice_case.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_epoch_revoked_during_confirmation_intent_does_not_store_pending(monkeypatch, voice_case):
    started = asyncio.Event()
    release = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(explicit_tts=False)

    monkeypatch.setattr("app.voice_intent.detect_tts_intent", suspended)
    voice_case.context.user_data.pop("voice_pending_10")
    task = asyncio.create_task(
        _show_confirmation_ui(
            voice_case.message,
            "Synthetic transcript",
            "ru",
            101,
            b"synthetic",
            SimpleNamespace(file_unique_id="synthetic-audio"),
            voice_case.context,
            memory_epoch=52,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.current.return_value = False
        release.set()
        await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert "voice_pending_10" not in voice_case.context.user_data


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["snapshot", "lease", "get_chat"])
async def test_callback_cancel_awaits_transcript_preparation_and_blocks_continuation(monkeypatch, voice_case, boundary):
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Even a dependency which returns after cancellation must not resume the operation.
            pass
        finally:
            cleaned.set()
        return voice_case.chat if boundary == "get_chat" else True

    @asynccontextmanager
    async def suspended_lease(*args, **kwargs):
        voice_case.lease_events.append("enter")
        try:
            yield await suspended()
        finally:
            voice_case.lease_events.append("exit")

    if boundary == "snapshot":
        voice_case.current.side_effect = suspended
    elif boundary == "lease":
        monkeypatch.setattr("app.repos.memory_consent.private_data_lease", suspended_lease)
    else:
        monkeypatch.setattr(cb_voice, "get_user_chat", suspended)
    voice_case.query.data = "voice:transcribe_only"
    first = asyncio.create_task(cb_voice.voice_callback(voice_case.update, voice_case.context))
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        await asyncio.wait_for(cb_voice.voice_callback(voice_case.update, voice_case.context), 1)
        cleaned_before_cancel_returned = cleaned.is_set()
        owner_done_before_cancel_returned = first.done()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        voice_case.dispatch.assert_not_awaited()
        assert cleaned_before_cancel_returned
        assert owner_done_before_cancel_returned
        assert voice_case.lock.locked() is False
        assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
        assert cb_voice._voice_operations == {}
        if boundary == "lease":
            assert voice_case.lease_events == ["enter", "exit"]
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_confirmation_ui_cancel_invalidates_owner_before_pending_write(monkeypatch, voice_case):
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        finally:
            cleaned.set()
        return SimpleNamespace(explicit_tts=False)

    monkeypatch.setattr("app.voice_intent.detect_tts_intent", suspended)
    voice_case.context.user_data.pop("voice_pending_10")
    first = asyncio.create_task(
        _show_confirmation_ui(
            voice_case.message,
            "Synthetic transcript",
            "ru",
            101,
            b"synthetic",
            SimpleNamespace(file_unique_id="synthetic-audio"),
            voice_case.context,
            memory_epoch=52,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        await asyncio.wait_for(cb_voice.voice_callback(voice_case.update, voice_case.context), 1)
        owner_cleaned_before_cancel_returned = cleaned.is_set() and first.done()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
        assert owner_cleaned_before_cancel_returned
        assert "Отменено" in voice_case.query.edit_message_text.await_args.args[0]
        assert cb_voice._voice_operations == {}
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search"])
async def test_callback_cancel_after_dispatch_awaits_worker_cleanup_before_success_ui(voice_case, action):
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    completed_effects = []

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await release.wait()
            completed_effects.append("downstream completion")
        finally:
            cleaned.set()

    voice_case.dispatch.side_effect = suspended
    voice_case.query.data = f"voice:{action}"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    owner = voice_case.pending["_task"]
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        await asyncio.wait_for(cb_voice.voice_callback(voice_case.update, voice_case.context), 1)
        owner_done_at_success_ui = owner.done() and cleaned.is_set()
        assert "Отменено" in voice_case.query.edit_message_text.await_args.args[0]
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        assert completed_effects == []
        assert owner_done_at_success_ui
        assert voice_case.lock.locked() is False
        assert voice_case.tasks == set()
        assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
        assert cb_voice._voice_operations == {}
    finally:
        release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_cancel_has_explicit_no_pending_result(voice_case):
    voice_case.context.user_data.pop("voice_pending_10")
    voice_case.query.data = "voice:cancel"
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    assert "Отменено" not in voice_case.query.edit_message_text.await_args.args[0]
    assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}


@pytest.mark.asyncio
async def test_repeated_callback_cancel_waits_once_for_worker_cleanup(voice_case):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    second_started = asyncio.Event()
    cleaned = asyncio.Event()
    cleanup_interruptions = []

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interruptions.append("second cancellation reached cleanup")
                raise
            finally:
                cleaned.set()

    async def cancel_again():
        second_started.set()
        await cb_voice.voice_callback(voice_case.update, voice_case.context)

    voice_case.dispatch.side_effect = suspended
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    owner = voice_case.pending["_task"]
    first_cancel = None
    second_cancel = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        first_cancel = asyncio.create_task(cb_voice.voice_callback(voice_case.update, voice_case.context))
        await asyncio.wait_for(cleanup_started.wait(), 1)
        second_cancel = asyncio.create_task(cancel_again())
        await asyncio.wait_for(second_started.wait(), 1)
        assert not first_cancel.done()
        assert not second_cancel.done()
        assert voice_case.lock.locked()
        assert "Отменено" not in voice_case.query.edit_message_text.await_args.args[0]
        release_cleanup.set()
        await asyncio.gather(first_cancel, second_cancel)
        assert owner.done() and cleaned.is_set()
        assert cleanup_interruptions == []
        assert voice_case.lock.locked() is False
        assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
        assert cb_voice._voice_operations == {}
    finally:
        release_cleanup.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        for cancel_task in (first_cancel, second_cancel):
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_wrong_source_cancel_does_not_cancel_dispatched_owner(voice_case):
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        finally:
            cleaned.set()

    voice_case.dispatch.side_effect = suspended
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    owner = voice_case.pending["_task"]
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.message.message_id = 99
        voice_case.query.data = "voice:cancel"
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
        assert not owner.done() and not cleaned.is_set()
        assert voice_case.lock.locked()
        assert "Отменено" not in voice_case.query.edit_message_text.await_args.args[0]
        assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
        release.set()
        await asyncio.gather(owner)
        assert cb_voice._voice_operations == {}
    finally:
        release.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_order", ["caller_first", "owner_first"])
async def test_cancel_callback_interruption_still_awaits_owned_cleanup(voice_case, completion_order):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    interrupted_cleanup = []

    async def suspended(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                interrupted_cleanup.append("interrupted cleanup")
                raise
            finally:
                cleaned.set()

    voice_case.dispatch.side_effect = suspended
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    owner = voice_case.pending["_task"]
    cancel_callback = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        voice_case.query.data = "voice:cancel"
        cancel_callback = asyncio.create_task(cb_voice.voice_callback(voice_case.update, voice_case.context))
        await asyncio.wait_for(cleanup_started.wait(), 1)
        reason = f"cancel callback interrupted: {completion_order}"
        if completion_order == "owner_first":
            # Queue owner completion before caller cancellation, without yielding between them.
            release_cleanup.set()
            cancel_callback.cancel(reason)
        else:
            cancel_callback.cancel(reason)
            release_cleanup.set()
        with pytest.raises(asyncio.CancelledError, match=reason) as cancelled:
            await cancel_callback
        assert cancelled.value.args == (reason,)
        assert cancel_callback.cancelled()
        assert cancel_callback.cancelling() == 1
        assert owner.done() and cleaned.is_set()
        assert interrupted_cleanup == []
        assert voice_case.lock.locked() is False
        assert voice_case.tasks == set()
        assert "Отменено" not in voice_case.query.edit_message_text.await_args.args[0]
        assert voice_case.context.user_data == {"voice_pending_20": voice_case.other}
        assert cb_voice._voice_operations == {}
    finally:
        release_cleanup.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        if cancel_callback is not None:
            cancel_callback.cancel()
            await asyncio.gather(cancel_callback, return_exceptions=True)
