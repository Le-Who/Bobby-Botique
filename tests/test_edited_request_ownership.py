"""Real message handoffs retain cancellation, heartbeat and input ownership."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.handlers import agent, cb_ai_actions, messages
from app.utils import heartbeat
from app.utils.background_tasks import TaskManager
from tests.factories import make_chat_state

pytestmark = pytest.mark.asyncio


async def checkpoint():
    ready = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(ready.set_result, None)
    await ready


async def settle():
    for _ in range(20):
        await checkpoint()


@pytest.fixture
async def ownership(monkeypatch):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=8))
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(state, "_LAST_BOT_MESSAGE", {})
    monkeypatch.setattr(state, "_NETWORK_STALL_SINCE", {})
    monkeypatch.setattr(state, "_schedule_persist", MagicMock())
    monkeypatch.setattr(state, "ensure_state_loaded", AsyncMock())
    monkeypatch.setattr(messages, "is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "check_user_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "api_logger", MagicMock(log_request=MagicMock(return_value=0)))
    monkeypatch.setattr(messages, "metrics_collector", SimpleNamespace(record_request=AsyncMock()))
    for name in (
        "process_media_group_update",
        "handle_role_rename",
        "handle_edit_prompt",
        "handle_conversation_rename",
        "handle_manual_role_input",
        "handle_custom_role_generation",
        "handle_document_mode_interaction",
    ):
        monkeypatch.setattr(messages, name, AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.board_handler.try_handle_board_reply", AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.cmd_image.handle_draw_prompt_input", AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.cmd_image.check_draw_intent_async", AsyncMock(return_value=None))
    monkeypatch.setattr("app.middleware.dedup.is_duplicate_request", AsyncMock(return_value=False))
    monkeypatch.setattr("app.intent_router.try_direct_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.voice_intent.detect_tts_intent", AsyncMock(return_value=SimpleNamespace(explicit_tts=False))
    )

    async def debounce(_uid, message, **_kwargs):
        return SimpleNamespace(
            build_llm_context=lambda: message.text,
            has_forwarded_content=False,
            forwarded_photo_messages=[],
            user_entries=[],
            forwarded_entries=[],
        )

    monkeypatch.setattr("app.middleware.debounce.debounce_message", debounce)
    monkeypatch.setattr(messages, "_send_busy_ephemeral", AsyncMock())
    # Replace only this router's scheduling delay with a controlled checkpoint.
    # The assertions below concern ownership after cleanup, not a timer duration.
    router_asyncio = SimpleNamespace(**vars(asyncio))
    router_asyncio.sleep = lambda _delay: checkpoint()
    monkeypatch.setattr(messages, "asyncio", router_asyncio)
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_EVENTS", {})
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_TASKS", {})
    monkeypatch.setattr(agent, "get_user_chat", AsyncMock(return_value=make_chat_state()))
    monkeypatch.setattr(agent, "heavy_request_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(agent, "ultra_heavy_semaphore", asyncio.Semaphore(4))
    owners = []
    workers = []
    submitted_coroutines = {}
    manager = TaskManager()
    heartbeat_tasks = []
    ui = []

    register = messages.register_heartbeat

    def register_heartbeat(message_id, event, chat):
        register(message_id, event, chat)
        heartbeat_tasks.append(heartbeat._HEARTBEAT_TASKS[message_id])

    monkeypatch.setattr(messages, "register_heartbeat", register_heartbeat)

    def submit(coro, **kwargs):
        task = manager.submit(coro, **kwargs)
        workers.append(task)
        submitted_coroutines[task] = coro
        return task

    monkeypatch.setattr(messages, "submit_task", submit)
    monkeypatch.setattr(messages, "try_submit_task", submit)
    contexts = {}
    placeholders = {}

    def context(uid=11):
        if uid not in contexts:
            placeholder = SimpleNamespace(message_id=uid * 100, delete=AsyncMock())

            async def edit_text(text, **_kwargs):
                ui.append((uid, "worker", text))
                return placeholder

            placeholder.edit_text = AsyncMock(side_effect=edit_text)

            async def edit_message_text(**kwargs):
                ui.append((uid, "replacement", kwargs["text"]))
                return placeholder

            placeholders[uid] = placeholder
            contexts[uid] = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(
                    username="synthetic_bot", edit_message_text=AsyncMock(side_effect=edit_message_text)
                ),
            )
        return contexts[uid]

    def update(text, *, edited=False, uid=11):
        context(uid)
        chat = SimpleNamespace(id=uid, type="private", send_action=AsyncMock())
        msg = SimpleNamespace(
            text=text,
            caption=None,
            photo=(),
            voice=None,
            document=None,
            media_group_id=None,
            reply_to_message=None,
            reply_text=AsyncMock(return_value=placeholders[uid]),
        )
        return SimpleNamespace(
            update_id=len(owners) + 1,
            effective_user=SimpleNamespace(id=uid),
            effective_chat=chat,
            effective_message=msg,
            message=None if edited else msg,
            edited_message=msg if edited else None,
        )

    def launch(item):
        handler = messages.handle_edited_request if item.edited_message else messages.handle_request
        owner = asyncio.create_task(handler(item, context(item.effective_user.id)))
        owners.append(owner)
        return owner

    try:
        yield SimpleNamespace(
            launch=launch,
            update=update,
            context=context,
            placeholders=placeholders,
            ui=ui,
            owners=owners,
            workers=workers,
            submitted_coroutines=submitted_coroutines,
            heartbeat_tasks=heartbeat_tasks,
        )
    finally:
        for task in owners + workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owners, *workers, return_exceptions=True)
        # Own captured inputs even when a RED assertion identifies an unstarted
        # coroutine that its production outer task failed to close.
        for coroutine in submitted_coroutines.values():
            coroutine.close()
        for task in heartbeat_tasks:
            task.cancel()
        await asyncio.gather(*heartbeat_tasks, return_exceptions=True)


@pytest.mark.parametrize("initial", ["ordinary", "edit"])
@pytest.mark.parametrize("old_terminal", ["cancel", "error"])
@pytest.mark.parametrize("next_cancel", ["edit", "button"])
async def test_superseded_cleanup_cannot_erase_replacement(monkeypatch, ownership, initial, old_terminal, next_cancel):
    old_started, cleanup_started, release_cleanup = (asyncio.Event() for _ in range(3))
    error_started, release_error, replacement_started = (asyncio.Event() for _ in range(3))
    third_started, other_started, release_other = (asyncio.Event() for _ in range(3))
    entered = []

    async def process(placeholder, update, context, text_override=None, **_kwargs):
        uid = update.effective_user.id
        text = text_override if text_override is not None else context.user_data.get("_edited_text_override")
        assert state.get_user_lock(uid).locked()
        entered.append((uid, text))
        if uid == 22:
            other_started.set()
            await release_other.wait()
        elif text == "original":
            old_started.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                if old_terminal == "error":
                    raise RuntimeError("synthetic old cleanup failed")
        elif text == "replacement":
            replacement_started.set()
            await asyncio.Future()
        elif text == "third":
            third_started.set()

    monkeypatch.setattr(agent, "process_long_request", process)
    try:
        await ownership.launch(ownership.update("original", edited=initial == "edit"))
        await old_started.wait()
        old_worker = state._ACTIVE_TASKS[11]
        placeholder = ownership.placeholders[11]
        original_edit = placeholder.edit_text.side_effect

        async def gated_error(text, **kwargs):
            error_started.set()
            await release_error.wait()
            return await original_edit(text, **kwargs)

        placeholder.edit_text.side_effect = gated_error
        replacement_handler = ownership.launch(ownership.update("replacement", edited=True))
        await cleanup_started.wait()
        # Allow all runnable owners to reach their next event. No elapsed-time oracle.
        await settle()
        await ownership.launch(ownership.update("other user", uid=22))
        await other_started.wait()
        other_worker = state._ACTIVE_TASKS[22]
        release_cleanup.set()
        if old_terminal == "error":
            await error_started.wait()
            await settle()
            release_error.set()
        await old_worker
        await replacement_handler
        await replacement_started.wait()

        replacement = state._ACTIVE_TASKS.get(11)
        assert replacement is not None, "superseded cleanup erased the replacement's cancellation entry"
        assert replacement is not old_worker and not replacement.done()
        assert state.get_user_state(11).is_processing, "superseded cleanup released the replacement's reservation"
        assert state.get_user_lock(11).locked()
        assert heartbeat.is_heartbeat_active(placeholder.message_id), "old cleanup stopped the reused heartbeat"
        assert ownership.heartbeat_tasks[0].done(), "old heartbeat task was orphaned by placeholder reuse"
        assert state._ACTIVE_TASKS[22] is other_worker and not other_worker.done()
        if old_terminal == "error":
            assert ownership.ui[-1][1] == "replacement", "old error overwrote the replacement's UI"

        if next_cancel == "edit":
            await ownership.launch(ownership.update("third", edited=True))
            await third_started.wait()
            await replacement
        else:
            query = SimpleNamespace(
                data=f"retry_wait:{placeholder.message_id}",
                message=SimpleNamespace(message_id=placeholder.message_id, chat=SimpleNamespace(id=11)),
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            await cb_ai_actions.cancel_long_wait_callback(
                SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), SimpleNamespace()
            )
            await replacement
            query.edit_message_text.assert_awaited_once()
        await settle()
        assert 11 not in state._ACTIVE_TASKS
        assert not state.get_user_state(11).is_processing
        assert not state.get_user_lock(11).locked()
        assert not heartbeat.is_heartbeat_active(placeholder.message_id)
        await ownership.launch(ownership.update("third"))
        await settle()
        assert entered[-1] == (11, "third")
        assert not state.get_user_state(11).is_processing
        assert 11 not in state._ACTIVE_TASKS
    finally:
        release_cleanup.set()
        release_error.set()
        release_other.set()


@pytest.mark.parametrize("finish", ["success", "cancel", "error"])
async def test_edited_text_is_request_scoped_after_every_exit(monkeypatch, ownership, finish):
    started = asyncio.Event()
    seen = []
    arguments = []
    real_process = agent.process_long_request

    async def chat(_placeholder, _uid, text, _chat_state, **_kwargs):
        seen.append(text)
        if text == "corrected" and finish == "cancel":
            started.set()
            await asyncio.Future()

    async def process(placeholder, update, context, text_override=None, **kwargs):
        arguments.append(text_override)
        await real_process(placeholder, update, context, text_override, **kwargs)
        if update.edited_message and finish == "error":
            raise RuntimeError("synthetic dispatch failure")

    monkeypatch.setattr(agent, "_handle_regular_chat", chat)
    monkeypatch.setattr(agent, "process_long_request", process)
    await ownership.launch(ownership.update(" corrected ", edited=True))
    worker = ownership.workers[-1]
    if finish == "cancel":
        await started.wait()
        assert state.cancel_active_task(11)
    await worker
    assert inspect.getcoroutinestate(ownership.submitted_coroutines[worker]) == inspect.CORO_CLOSED
    assert "_edited_text_override" not in ownership.context().user_data, "edited input leaked after worker exit"
    assert arguments == ["corrected"], "edited dispatch must use the existing per-request text override"
    await ownership.launch(ownership.update("ordinary follows"))
    await settle()
    assert seen == ["corrected", "ordinary follows"]
    assert arguments == ["corrected", "ordinary follows"]
    assert not state.get_user_state(11).is_processing
    assert 11 not in state._ACTIVE_TASKS


async def test_legacy_agent_override_call_remains_compatible(monkeypatch, ownership):
    chat = AsyncMock()
    monkeypatch.setattr(agent, "_handle_regular_chat", chat)
    context = ownership.context()
    context.user_data["_edited_text_override"] = "legacy corrected input"
    update = ownership.update("original input")
    await agent.process_long_request(ownership.placeholders[11], update, context)
    assert chat.await_args.args[2] == "legacy corrected input"


async def test_rapid_edits_keep_latest_admission_owned_until_predecessor_drains(monkeypatch, ownership):
    started, cleanup_started, release_cleanup, latest_started = (asyncio.Event() for _ in range(4))
    seen = []

    async def process(_placeholder, update, context, text_override=None, **_kwargs):
        text = text_override if text_override is not None else context.user_data.get("_edited_text_override")
        seen.append(text)
        if text == "original":
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
        else:
            assert text == "latest", "a superseded admission started provider work"
            latest_started.set()
            await asyncio.Future()

    monkeypatch.setattr(agent, "process_long_request", process)
    try:
        await ownership.launch(ownership.update("original"))
        await started.wait()
        old = state._ACTIVE_TASKS[11]
        middle = ownership.launch(ownership.update("middle", edited=True))
        await cleanup_started.wait()
        await settle()
        latest = ownership.launch(ownership.update("latest", edited=True))
        await settle()
        assert not latest_started.is_set(), "new work started before predecessor cleanup"
        assert state.get_user_state(11).is_processing
        release_cleanup.set()
        await old
        await asyncio.gather(middle, return_exceptions=True)
        await latest
        await latest_started.wait()
        worker = state._ACTIVE_TASKS.get(11)
        assert worker is not None, "latest edit became unreachable after the cleanup chain"
        assert heartbeat.is_heartbeat_active(ownership.placeholders[11].message_id)
        assert state.cancel_active_task(11)
        await worker
        assert seen == ["original", "latest"]
        assert not state.get_user_state(11).is_processing
        assert 11 not in state._ACTIVE_TASKS
        assert not heartbeat._HEARTBEAT_EVENTS
    finally:
        release_cleanup.set()


@pytest.mark.parametrize("edited", [False, True])
async def test_worker_cancelled_before_first_step_releases_owned_resources(monkeypatch, ownership, edited):
    register = state.register_active_task
    downstream = AsyncMock()
    monkeypatch.setattr(agent, "process_long_request", downstream)
    cancelled_workers = []

    def register_and_cancel(uid, task):
        register(uid, task)
        if task is not asyncio.current_task():
            cancelled_workers.append(task)
            task.cancel("cancel before first worker step")

    monkeypatch.setattr(state, "register_active_task", register_and_cancel)
    await ownership.launch(ownership.update("request", edited=edited))
    await asyncio.gather(*cancelled_workers, return_exceptions=True)
    await settle()
    downstream.assert_not_awaited()
    assert not state.get_user_state(11).is_processing, "unstarted worker leaked its admission"
    assert not state.get_user_lock(11).locked()
    assert 11 not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
    assert cancelled_workers
    for task in cancelled_workers:
        coroutine = ownership.submitted_coroutines[task]
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED, (
            "cancelled outer task leaked its inner worker"
        )


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_edited_placeholder_failure_releases_admission(monkeypatch, ownership, failure):
    started, release, finished = (asyncio.Event() for _ in range(3))
    state.set_last_bot_message(11, 1100, 11)
    downstream = AsyncMock()
    monkeypatch.setattr(agent, "process_long_request", downstream)

    async def edit_placeholder(**_kwargs):
        started.set()
        try:
            await release.wait()
            if failure == "error":
                raise RuntimeError("synthetic edit delivery error")
            await asyncio.Future()
        finally:
            finished.set()

    ownership.context().bot.edit_message_text.side_effect = edit_placeholder
    owner = ownership.launch(ownership.update("corrected text", edited=True))
    await started.wait()
    assert state.get_user_state(11).is_processing
    assert state.get_active_task(11) is owner
    if failure == "cancel":
        owner.cancel("edited admission cancellation")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("edited admission cancellation",)
    else:
        release.set()
        with pytest.raises(RuntimeError, match="synthetic edit delivery error"):
            await owner
    assert finished.is_set()
    downstream.assert_not_awaited()
    assert not state.get_user_state(11).is_processing
    assert 11 not in state._ACTIVE_TASKS
    assert not state.get_user_lock(11).locked()
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
    await ownership.launch(ownership.update("next ordinary request"))
    await settle()
    downstream.assert_awaited_once()
    assert downstream.await_args.kwargs["text_override"] == "next ordinary request"


async def test_cancelling_edit_admission_keeps_predecessor_cleanup_owned(monkeypatch, ownership):
    started, cleanup_started, release_cleanup, cleanup_finished = (asyncio.Event() for _ in range(4))
    interrupted = []
    calls = []

    async def process(_placeholder, _update, _context, *, text_override, **_kwargs):
        calls.append(text_override)
        if text_override == "original":
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                try:
                    await release_cleanup.wait()
                    cleanup_finished.set()
                except asyncio.CancelledError:
                    interrupted.append(True)
                    raise

    monkeypatch.setattr(agent, "process_long_request", process)
    try:
        await ownership.launch(ownership.update("original"))
        await started.wait()
        old_worker = state.get_active_task(11)
        owner = ownership.launch(ownership.update("cancelled edit", edited=True))
        await cleanup_started.wait()
        owner.cancel("original edit cancellation")
        await settle()
        owner.cancel("later edit cancellation")
        await settle()
        assert not owner.done()
        assert not interrupted
        active = state.get_active_task(11)
        assert active is not None and active is not old_worker and not active.done()
        assert state.get_user_state(11).is_processing
        ownership.context().bot.edit_message_text.assert_not_awaited()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("original edit cancellation",)
        await old_worker
        assert cleanup_finished.is_set()
        assert calls == ["original"]
        assert 11 not in state._ACTIVE_TASKS
        assert not state.get_user_state(11).is_processing
        assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
        await ownership.launch(ownership.update("next request"))
        await settle()
        assert calls == ["original", "next request"]
        assert 11 not in state._ACTIVE_TASKS
    finally:
        release_cleanup.set()


async def test_completed_prestart_cleanup_cannot_stop_replacement_heartbeat(monkeypatch, ownership):
    intent_started, release_intent, edit_preflight_started, release_preflight, replacement_started = (
        asyncio.Event() for _ in range(5)
    )
    seen = []
    cancelled = []
    rate_count = 0
    register = state.register_active_task

    async def intent(_text):
        intent_started.set()
        await release_intent.wait()

    async def rate_limit(_uid):
        nonlocal rate_count
        rate_count += 1
        if rate_count == 2:
            edit_preflight_started.set()
            await release_preflight.wait()
        return True

    async def process(_placeholder, _update, _context, *, text_override, **_kwargs):
        seen.append(text_override)
        replacement_started.set()
        await asyncio.Future()

    def register_and_cancel(uid, task):
        register(uid, task)
        if task is not asyncio.current_task() and not cancelled:
            cancelled.append(task)
            task.cancel("synthetic pre-start cancellation")

    monkeypatch.setattr(agent, "process_long_request", process)
    monkeypatch.setattr(messages, "check_user_rate_limit", rate_limit)
    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    monkeypatch.setattr(state, "register_active_task", register_and_cancel)
    try:
        original = ownership.launch(ownership.update("original"))
        await intent_started.wait()
        edit = ownership.launch(ownership.update("replacement", edited=True))
        await edit_preflight_started.wait()
        # Queue the edit continuation after worker completion but before its
        # resource callback, using real handler preflight and task submission.
        release_intent.set()
        asyncio.get_running_loop().call_soon(release_preflight.set)
        await original
        await edit
        await replacement_started.wait()
        active = state.get_active_task(11)
        assert seen == ["replacement"]
        assert active is not None and active is not cancelled[0] and not active.done()
        assert state.get_user_state(11).is_processing
        assert state.get_user_lock(11).locked()
        assert inspect.getcoroutinestate(ownership.submitted_coroutines[cancelled[0]]) == inspect.CORO_CLOSED
        assert heartbeat.is_heartbeat_active(ownership.placeholders[11].message_id), (
            "completed predecessor callback stopped the replacement heartbeat"
        )
        assert state.cancel_active_task(11)
        await active
        await settle()
        assert 11 not in state._ACTIVE_TASKS
        assert not state.get_user_state(11).is_processing
        assert not state.get_user_lock(11).locked()
        assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
    finally:
        release_intent.set()
        release_preflight.set()
