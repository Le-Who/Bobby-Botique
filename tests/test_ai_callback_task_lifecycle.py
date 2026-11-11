"""AI callbacks join shared shutdown drain and reject unavailable capacity."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.handlers import cb_ai_actions, retry_requests
from app.utils.background_tasks import TaskManager
from tests import test_request_concurrency as request_support
from tests.factories import make_chat_state, make_telegram_context, make_telegram_update

pytestmark = pytest.mark.asyncio
request_boundary = request_support.request_boundary


@pytest.fixture
async def callback_work(monkeypatch):
    uid = 12345
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=8))
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(state, "_LAST_BOT_MESSAGE", {})
    monkeypatch.setattr(state, "_schedule_persist", MagicMock())
    monkeypatch.setattr(state, "ensure_state_loaded", AsyncMock())
    chat = make_chat_state()
    started, finished = asyncio.Event(), asyncio.Event()
    calls = []

    async def work(*_args, **_kwargs):
        calls.append(True)
        started.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", work)
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_complex_agent_search", work)
    monkeypatch.setattr(cb_ai_actions, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.adapters.concurrency.ultra_heavy_semaphore", asyncio.Semaphore(2))
    monkeypatch.setattr(cb_ai_actions, "_HEAVY_CALLBACK_SEMAPHORE", asyncio.Semaphore(2))
    monkeypatch.setattr(
        "app.voice_intent.detect_tts_intent", AsyncMock(return_value=SimpleNamespace(explicit_tts=False))
    )
    manager, tasks = TaskManager(), set()
    monkeypatch.setattr(cb_ai_actions, "try_submit_task", manager.try_submit, raising=False)
    monkeypatch.setattr(cb_ai_actions, "_background_tasks", tasks)
    placeholder = SimpleNamespace(message_id=201, chat=SimpleNamespace(id=uid), edit_text=AsyncMock())
    message = SimpleNamespace(
        message_id=200,
        chat=SimpleNamespace(id=uid),
        text="A sufficiently long partial response",
        reply_text=AsyncMock(return_value=placeholder),
        edit_text=AsyncMock(),
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=uid),
            caption="? question",
            text="question",
            message_id=17,
            chat=SimpleNamespace(id=uid),
        ),
    )
    query = SimpleNamespace(from_user=SimpleNamespace(id=uid), id="callback", message=message, answer=AsyncMock())

    async def launch(kind, *, original=None):
        query.data = {
            "complex": "complex_search:confirm",
            "fallback": "fallback:confirm:model",
            "continue": "continue_stream",
            "retry": "",
        }[kind]
        if kind == "retry":
            request = retry_requests.begin_request(uid, "chat", "question", chat)
            request.response_ref = (message.message_id, uid)
            query.data = request.callback_data
        handler = {
            "complex": cb_ai_actions.complex_search_callback,
            "fallback": cb_ai_actions.fallback_callback,
            "continue": cb_ai_actions.continue_stream_callback,
            "retry": cb_ai_actions.retry_last_callback,
        }[kind]
        await handler(
            SimpleNamespace(callback_query=query),
            SimpleNamespace(user_data={"original_message": original} if original else {}),
        )

    try:
        yield SimpleNamespace(
            launch=launch,
            manager=manager,
            tasks=tasks,
            started=started,
            finished=finished,
            calls=calls,
            message=message,
            placeholder=placeholder,
            uid=uid,
            query=query,
            chat=chat,
        )
    finally:
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
        await manager.drain(timeout=0, cancel_timeout=1)


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue", "retry"])
async def test_callback_worker_is_cancelled_and_joined_by_shared_drain(callback_work, kind):
    await callback_work.launch(kind)
    await asyncio.wait_for(callback_work.started.wait(), 1)
    assert await callback_work.manager.drain(timeout=0, cancel_timeout=1)
    assert callback_work.finished.is_set(), "Accepted callback escaped shutdown drain"
    assert all(task.done() for task in callback_work.tasks)
    assert not state.get_user_lock(callback_work.uid).locked()


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue", "retry"])
async def test_callback_capacity_rejection_has_visible_feedback_and_no_worker(callback_work, kind):
    callback_work.manager.MAX_TASKS = 0
    await callback_work.launch(kind)
    await asyncio.sleep(0)
    assert not callback_work.calls
    assert not callback_work.tasks
    edits = callback_work.placeholder.edit_text.await_args_list + callback_work.message.edit_text.await_args_list
    assert any("перегружен" in str(call) or "overloaded" in str(call).lower() for call in edits)
    assert not state.get_user_lock(callback_work.uid).locked()


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue"])
@pytest.mark.parametrize("phase", ["answer", "semaphore"])
async def test_callback_reserves_user_before_ui_or_shared_capacity_wait(
    monkeypatch, request_boundary, callback_work, kind, phase
):
    entered, release = asyncio.Event(), asyncio.Event()
    semaphore = asyncio.Semaphore(0)
    owner = None
    if phase == "answer":
        count = 0

        async def answer(*_args, **_kwargs):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()

        callback_work.query.answer.side_effect = answer
        owner = asyncio.create_task(callback_work.launch(kind))
        await asyncio.wait_for(entered.wait(), 1)
    else:
        monkeypatch.setattr(cb_ai_actions, "_HEAVY_CALLBACK_SEMAPHORE", semaphore)
        monkeypatch.setattr("app.adapters.concurrency.ultra_heavy_semaphore", semaphore)
        await callback_work.launch(kind)
        await asyncio.sleep(0)
    try:
        await callback_work.launch(kind)
        update = make_telegram_update("competing request", user_id=callback_work.uid, chat_id=callback_work.uid)
        await request_support.messages.handle_request(update, make_telegram_context())
        request_boundary.busy.assert_awaited_once_with(update)
        assert state.get_user_lock(callback_work.uid).locked(), "Callback admission is unreserved while waiting"
        assert state.get_user_state(callback_work.uid).is_processing
        assert not callback_work.calls
        assert len(callback_work.tasks) == (0 if phase == "answer" else 1)
    finally:
        release.set()
        semaphore.release()
        if owner is not None:
            await owner


@pytest.mark.parametrize("kind", ["complex", "fallback"])
async def test_callback_uses_its_reply_source_instead_of_newer_shared_slot(monkeypatch, callback_work, kind):
    original = callback_work.message.reply_to_message
    original.text = "OLD source"
    newer = SimpleNamespace(from_user=SimpleNamespace(id=callback_work.uid), text="NEW source", caption="? NEW")
    downstream = AsyncMock()
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", downstream)
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_complex_agent_search", downstream)
    await callback_work.launch(kind, original=newer)
    assert await callback_work.manager.drain(timeout=1)
    assert downstream.await_args.args[1 if kind == "complex" else 2] == (
        original if kind == "complex" else "OLD source"
    )


@pytest.mark.parametrize("kind", ["complex", "fallback"])
@pytest.mark.parametrize("binding", ["owned", "other_response", "other_source"])
async def test_legacy_shared_source_requires_exact_response_ownership(monkeypatch, callback_work, kind, binding):
    uid = callback_work.uid
    original = callback_work.message.reply_to_message
    callback_work.message.reply_to_message = None
    state.set_last_bot_message(uid, 999 if binding == "other_response" else callback_work.message.message_id, uid)
    retry_requests.remember_message_request(
        uid, 18 if binding == "other_source" else original.message_id, uid, asyncio.current_task()
    )
    retry_requests.message_request(uid).response_ref = (callback_work.message.message_id, uid)
    downstream = AsyncMock()
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", downstream)
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_complex_agent_search", downstream)
    await callback_work.launch(kind, original=original)
    assert await callback_work.manager.drain(timeout=1)
    assert downstream.await_count == int(binding == "owned")
    if binding != "owned":
        assert not callback_work.tasks


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue"])
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_callback_ui_failure_releases_reserved_user(callback_work, kind, failure):
    async def answer(*_args, **_kwargs):
        assert state.get_user_lock(callback_work.uid).locked()
        if failure == "cancel":
            raise asyncio.CancelledError("synthetic callback cancellation")
        raise RuntimeError("synthetic callback UI failure")

    callback_work.query.answer.side_effect = answer
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await callback_work.launch(kind)
    assert not callback_work.tasks
    assert not state.get_user_lock(callback_work.uid).locked()
    assert not state.get_user_state(callback_work.uid).is_processing
    assert state.get_active_task(callback_work.uid) is None


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue"])
async def test_unstarted_callback_worker_releases_its_reservation(monkeypatch, callback_work, kind):
    register = state.register_active_task

    def cancel_worker(uid, task):
        register(uid, task)
        if task is not asyncio.current_task():
            task.cancel("cancel before first worker step")

    monkeypatch.setattr(state, "register_active_task", cancel_worker)
    await callback_work.launch(kind)
    assert await callback_work.manager.drain(timeout=1)
    assert not callback_work.calls
    assert not state.get_user_lock(callback_work.uid).locked()
    assert not state.get_user_state(callback_work.uid).is_processing
    assert state.get_active_task(callback_work.uid) is None


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue", "retry"])
async def test_callback_rejects_running_interaction_without_touching_its_owner(callback_work, kind):
    interaction = state.begin_interaction(callback_work.uid, "role")
    entered = asyncio.Event()

    async def generate():
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(state.run_interaction(interaction, generate))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await callback_work.launch(kind)
        assert not callback_work.tasks
        assert not callback_work.calls
        assert interaction.current() and not task.done()
        assert "Дождитесь" in callback_work.query.answer.await_args.args[0]
    finally:
        await state.cancel_interaction(callback_work.uid)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["complex", "fallback", "continue", "retry"])
async def test_callback_preserves_completed_role_preview(callback_work, kind):
    interaction = state.begin_interaction(callback_work.uid, "role")
    state.get_user_state(callback_work.uid).generated_role = {"title": "Preview"}
    await callback_work.launch(kind)
    await asyncio.wait_for(callback_work.started.wait(), 1)
    assert interaction.current()
    assert state.get_user_state(callback_work.uid).generated_role == {"title": "Preview"}


@pytest.mark.parametrize("changed", ["newer_response", "reset", "branch", "generation"])
async def test_stale_continuation_cannot_continue_current_history_or_replace_ownership(
    monkeypatch, callback_work, changed
):
    work = callback_work
    request = retry_requests.begin_request(work.uid, "chat", "Current question", work.chat)
    work.chat.history = [
        {"role": "user", "parts": ["Current question"]},
        {"role": "model", "parts": ["Current answer"]},
    ]
    response_id = 300 if changed == "newer_response" else work.message.message_id
    request.response_ref = (response_id, work.uid)
    retry_requests.remember_message_request(
        work.uid, 18 if changed == "newer_response" else 17, work.uid, asyncio.current_task()
    )
    source = retry_requests.message_request(work.uid)
    source.response_ref = request.response_ref
    state.set_last_bot_message(work.uid, response_id, work.uid)
    if changed == "reset":
        state.clear_retry_request(work.uid)
        work.chat.history = []
    elif changed == "branch":
        work.chat.branch_id = "new-branch"
    elif changed == "generation":
        work.chat.memory_epoch += 1
    before = list(work.chat.history)
    downstream = AsyncMock()
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", downstream)
    await work.launch("continue")
    assert await work.manager.drain(timeout=1)
    downstream.assert_not_awaited()
    assert work.chat.history == before
    assert state.get_last_bot_message(work.uid) == (response_id, work.uid)
    assert source.response_ref == (response_id, work.uid)
    work.message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("delivery", ["placeholder", "split_receipt", "replacement_receipt"])
async def test_matching_continuation_keeps_its_owned_logical_scope(monkeypatch, callback_work, delivery):
    work = callback_work
    retry_requests.remember_message_request(work.uid, 17, work.uid, asyncio.current_task())
    state.register_active_task(work.uid, asyncio.current_task())
    request = retry_requests.begin_request(work.uid, "chat", "question", work.chat)
    state.clear_active_task(work.uid)
    work.chat.history = [{"role": "user", "parts": ["question"]}, {"role": "model", "parts": ["Partial answer"]}]
    placeholder_ref = (work.message.message_id, work.uid)
    retry_requests.message_request(work.uid).response_ref = placeholder_ref
    state.set_last_bot_message(work.uid, *placeholder_ref)
    if delivery != "placeholder":
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from app.response_delivery.renderer import TelegramMessageRef, TelegramRenderer
        from tests.test_telegram_renderer import RecordingTransport

        transport = RecordingTransport(edit_failures=int(delivery == "replacement_receipt"))
        transport.current_ref = TelegramMessageRef(chat_id=work.uid, message_id=placeholder_ref[0])
        renderer = TelegramRenderer(
            transport,
            message_limit=50 if delivery == "split_receipt" else 4000,
            webapp_base_url="",
            telegraph_publication_enabled=False,
        )
        receipt = await renderer.open().finalize(
            displayed_text="Partial answer " * (20 if delivery == "split_receipt" else 1),
            title="Partial",
            actions=InlineKeyboardMarkup([[InlineKeyboardButton("Continue", callback_data="continue_stream")]]),
        )
        assert receipt.final_message.message_id != placeholder_ref[0]
        assert transport.sends[-1]["reply_markup"].inline_keyboard[0][0].callback_data == "continue_stream"
        retry_requests.bind_response(request, SimpleNamespace(receipt=receipt))
        work.message.message_id = receipt.final_message.message_id
        work.message.reply_to_message = SimpleNamespace(
            message_id=receipt.final_message.message_id - 1,
            chat=SimpleNamespace(id=work.uid),
            from_user=SimpleNamespace(id=99999),
        )
    else:
        request.response_ref = (work.message.message_id, work.uid)
    downstream = AsyncMock()
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", downstream)
    await work.launch("continue")
    assert await work.manager.drain(timeout=1)
    downstream.assert_awaited_once()
    assert downstream.await_args.args[3] is work.chat
    assert len(work.chat.history) == 2


async def test_newer_direct_intent_source_invalidates_old_continuation(monkeypatch, callback_work):
    work = callback_work
    retry_requests.remember_message_request(work.uid, 17, work.uid, asyncio.current_task())
    state.register_active_task(work.uid, asyncio.current_task())
    request = retry_requests.begin_request(work.uid, "chat", "question", work.chat)
    state.clear_active_task(work.uid)
    request.response_ref = (work.message.message_id, work.uid)
    assert request.source_ref == (17, work.uid)
    retry_requests.remember_message_request(work.uid, 18, work.uid, asyncio.current_task())
    newer_source = retry_requests.message_request(work.uid)
    newer_source.response_ref = (300, work.uid)
    state.set_last_bot_message(work.uid, 300, work.uid)
    downstream = AsyncMock()
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", downstream)
    await work.launch("continue")
    assert await work.manager.drain(timeout=1)
    downstream.assert_not_awaited()
    assert state.get_last_bot_message(work.uid) == (300, work.uid)
    assert retry_requests.message_request(work.uid) is newer_source


async def test_background_request_cannot_inherit_foreground_source(callback_work):
    work = callback_work
    retry_requests.remember_message_request(work.uid, 17, work.uid, asyncio.current_task())
    state.register_active_task(work.uid, asyncio.current_task())

    async def background():
        return retry_requests.begin_request(work.uid, "chat", "Scheduled question", work.chat)

    try:
        request = await asyncio.create_task(background())
        assert request.source_ref is None
    finally:
        state.clear_active_task(work.uid)


async def test_continuation_rechecks_logical_scope_after_shared_capacity_wait(monkeypatch, callback_work):
    work = callback_work
    request = retry_requests.begin_request(work.uid, "chat", "question", work.chat)
    work.chat.history = [{"role": "user", "parts": ["question"]}, {"role": "model", "parts": ["Partial answer"]}]
    request.response_ref = (work.message.message_id, work.uid)
    state.set_last_bot_message(work.uid, *request.response_ref)
    semaphore = asyncio.Semaphore(0)
    monkeypatch.setattr(cb_ai_actions, "_HEAVY_CALLBACK_SEMAPHORE", semaphore)
    downstream = AsyncMock()
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", downstream)
    await work.launch("continue")
    state.clear_retry_request(work.uid)
    work.chat.history = []
    semaphore.release()
    assert await work.manager.drain(timeout=1)
    downstream.assert_not_awaited()
    assert work.chat.history == []
