"""Cooperative notice retirement at real message and delivery handoffs."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, NetworkError

from app import state
from app.errors import ErrorCode
from app.handlers import agent, ai_chat, ai_search, cb_ai_actions, messages
from app.i18n import t
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
    KeyDisposition,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    TokenUsage,
)
from app.response_delivery.delivery import CompletedResponse, TelegramResponseDelivery, TelegramTarget
from app.response_delivery.presentation import FixedPresentation
from app.response_delivery.renderer import TelegramRenderer
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


def completion():
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=3),
        grounding=GroundingReport(),
        route=RouteUsed(ProviderKind.GEMINI, "synthetic", "synthetic"),
    )


def buttons(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row] if markup else []


class Message:
    def __init__(self):
        self.message_id = 1100
        self.chat_id = 11
        self.chat = SimpleNamespace(id=11, type="private")
        self.reply_to_message = None
        self.message_thread_id = None
        self.visible = {"text": "thinking", "reply_markup": None}
        self.writes = []
        self.notice_entered = asyncio.Event()
        self.release_notice = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.notice_finished = asyncio.Event()
        self.gate_notice = True
        self.cleanup_error = False
        self.notice_error = None

    def get_bot(self):
        return None

    async def edit_text(self, text, **kwargs):
        if "retry_wait:1100" in buttons(kwargs.get("reply_markup")):
            self.notice_entered.set()
            try:
                if self.notice_error:
                    raise self.notice_error
                if self.gate_notice:
                    try:
                        await self.release_notice.wait()
                    except asyncio.CancelledError:
                        self.cleanup_entered.set()
                        await self.release_cleanup.wait()
                        if self.cleanup_error:
                            raise RuntimeError("synthetic notice cleanup failure")
                        raise
            finally:
                self.notice_finished.set()
        self.visible = {"text": text, **kwargs}
        self.writes.append(self.visible)
        return self

    async def reply_text(self, text, **kwargs):
        return await self.edit_text(text, **kwargs)

    async def delete(self):
        pass


class Router:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.events = [TextDelta("A synthetic answer long enough for a progressive edit."), completion()]
        self.requests = []

    async def stream(self, request):
        assert isinstance(request, GenerationRequest)
        assert state.get_user_lock(11).locked()
        self.requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
            for event in self.events:
                yield event
        finally:
            self.closed.set()


@pytest.fixture
async def request_flow(monkeypatch):
    for name, value in (
        ("USER_STATES", state._UserStateStore(maxsize=8)),
        ("_ACTIVE_TASKS", {}),
        ("_LAST_BOT_MESSAGE", {}),
        ("_NETWORK_STALL_SINCE", {}),
    ):
        monkeypatch.setattr(state, name, value)
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
    for path in (
        "app.handlers.board_handler.try_handle_board_reply",
        "app.handlers.cmd_image.handle_draw_prompt_input",
        "app.middleware.dedup.is_duplicate_request",
    ):
        monkeypatch.setattr(path, AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.cmd_image.check_draw_intent_async", AsyncMock(return_value=None))
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
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_EVENTS", {})
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_TASKS", {})
    if hasattr(heartbeat, "_HEARTBEAT_OWNERS"):
        monkeypatch.setattr(heartbeat, "_HEARTBEAT_OWNERS", {})
    if hasattr(heartbeat, "_WAIT_NOTICE_TASKS"):
        monkeypatch.setattr(heartbeat, "_WAIT_NOTICE_TASKS", {})
    chat_state = make_chat_state(ltm_enabled=False)
    monkeypatch.setattr(agent, "get_user_chat", AsyncMock(return_value=chat_state))
    monkeypatch.setattr(agent, "heavy_request_semaphore", asyncio.Semaphore(4))
    monkeypatch.setattr(ai_chat, "ensure_chat_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(
        ai_chat, "_resolve_ai_request", AsyncMock(return_value=({"api_key": "synthetic"}, chat_state.model, "direct"))
    )
    monkeypatch.setattr(
        "app.process_policies.resolve_process",
        AsyncMock(
            return_value=SimpleNamespace(explicit=False, models=(chat_state.model,), strategy="legacy", revision=0)
        ),
    )
    monkeypatch.setattr(ai_chat, "update_user_chat", AsyncMock())
    monkeypatch.setattr("app.repos.memory.bind_retrieved_edges_to_response", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.metrics.metrics_collector.record_api_call", AsyncMock())
    monkeypatch.setattr("app.metrics.metrics_collector.record_request", AsyncMock())
    monkeypatch.setattr("app.model_selector.select_model", lambda *args, **kwargs: None)

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    message, router = Message(), Router()
    delivery = TelegramResponseDelivery(
        router, renderer_factory=partial(TelegramRenderer, telegraph_publication_enabled=False)
    )
    monkeypatch.setattr("app.response_delivery.delivery.get_telegram_response_delivery", lambda: delivery)
    threshold, delay_entered = asyncio.Event(), asyncio.Event()
    inputs, tasks, owners, typing = {}, [], [], []
    manager = TaskManager()
    scheduling = SimpleNamespace(cancel_before_start=None)

    async def controlled_wait(awaitable, *, timeout):
        if timeout != 60.0:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        delay_entered.set()
        stopped = asyncio.create_task(awaitable)
        elapsed = asyncio.create_task(threshold.wait())
        try:
            done, _ = await asyncio.wait({stopped, elapsed}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                return stopped.result()
            raise TimeoutError
        finally:
            for item in (stopped, elapsed):
                if not item.done():
                    item.cancel()
            await asyncio.gather(stopped, elapsed, return_exceptions=True)

    proxy = SimpleNamespace(**vars(asyncio))
    proxy.wait_for = controlled_wait
    monkeypatch.setattr(messages, "asyncio", proxy)
    register = messages.register_heartbeat

    def register_heartbeat(mid, event, chat):
        register(mid, event, chat)
        typing.append(heartbeat._HEARTBEAT_TASKS[mid])

    monkeypatch.setattr(messages, "register_heartbeat", register_heartbeat)

    def submit(coroutine, **kwargs):
        task = manager.submit(coroutine, **kwargs)
        tasks.append(task)
        inputs[task] = coroutine
        if coroutine.cr_code.co_name == "observed_worker":
            inputs[(task, "inner")] = coroutine.cr_frame.f_locals["coroutine"]
        if coroutine.__name__ == scheduling.cancel_before_start:
            task.cancel()
        return task

    monkeypatch.setattr(messages, "submit_task", submit)
    monkeypatch.setattr(messages, "try_submit_task", submit)
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(username="synthetic_bot"))

    async def edit_message_text(**kwargs):
        return await message.edit_text(kwargs.pop("text"), **kwargs)

    context.bot.edit_message_text = edit_message_text

    def update(text="hello", *, edited=False):
        incoming = SimpleNamespace(
            text=text,
            caption=None,
            photo=(),
            voice=None,
            document=None,
            media_group_id=None,
            reply_to_message=None,
            reply_text=AsyncMock(return_value=message),
        )
        return SimpleNamespace(
            update_id=len(owners) + 1,
            effective_user=SimpleNamespace(id=11),
            effective_chat=SimpleNamespace(id=11, type="private", send_action=AsyncMock()),
            effective_message=incoming,
            message=None if edited else incoming,
            edited_message=incoming if edited else None,
        )

    def launch(*, edited=False, text="hello"):
        handler = messages.handle_edited_request if edited else messages.handle_request
        owner = asyncio.create_task(handler(update(text, edited=edited), context))
        owners.append(owner)
        return owner

    def callback():
        query = SimpleNamespace(
            data="retry_wait:1100", message=message, answer=AsyncMock(), edit_message_text=message.edit_text
        )
        owner = asyncio.create_task(
            cb_ai_actions.cancel_long_wait_callback(
                SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), context
            )
        )
        owners.append(owner)
        return owner, query

    try:
        yield SimpleNamespace(
            message=message,
            router=router,
            delivery=delivery,
            threshold=threshold,
            delay_entered=delay_entered,
            launch=launch,
            callback=callback,
            manager=manager,
            tasks=tasks,
            inputs=inputs,
            owners=owners,
            scheduling=scheduling,
            chat_state=chat_state,
        )
    finally:
        message.release_notice.set()
        message.release_cleanup.set()
        router.release.set()
        threshold.set()
        for task in owners + tasks + typing:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owners, *tasks, *typing, return_exceptions=True)
        for coroutine in inputs.values():
            coroutine.close()


async def start_notice(flow):
    await flow.launch()
    await flow.router.started.wait()
    worker = state.get_active_task(11)
    flow.threshold.set()
    await flow.message.notice_entered.wait()
    return worker


@pytest.mark.parametrize("terminal", ["text", "failure", "deferred", "empty"])
async def test_real_delivery_joins_notice_before_successor_write(request_flow, terminal):
    """Removing the handoff join permits answer/terminal UI during notice cleanup."""
    flow = request_flow
    if terminal == "failure":
        flow.router.events = [
            StreamFailed(
                ErrorCode.TIMEOUT,
                FailurePhase.BEFORE_TEXT,
                RetryDisposition.DO_NOT_RETRY,
                KeyDisposition.UNCHANGED,
                "synthetic timeout",
            )
        ]
    elif terminal == "deferred":
        flow.router.events = [StreamDeferred("synthetic-task")]
    elif terminal == "empty":
        flow.router.events = [completion()]
    worker = await start_notice(flow)
    writes = len(flow.message.writes)
    flow.router.release.set()
    await settle()
    assert flow.message.cleanup_entered.is_set(), "delivery did not cancel its in-flight long-wait notice"
    assert len(flow.message.writes) == writes, "successor UI overtook notice cleanup"
    assert not worker.done()
    flow.message.release_cleanup.set()
    await worker
    await settle()
    assert flow.message.notice_finished.is_set()
    assert "retry_wait:1100" not in buttons(flow.message.visible.get("reply_markup"))
    assert "retry_last" in buttons(flow.message.visible.get("reply_markup"))
    if terminal == "text":
        assert "synthetic answer" in flow.message.visible["text"]
        assert "tts_reply" in buttons(flow.message.visible["reply_markup"])
    else:
        assert "synthetic answer" not in flow.message.visible["text"]
        assert {
            "failure": "Превышено время ожидания",
            "deferred": "временно перегружены",
            "empty": "Пустой ответ от модели",
        }[terminal] in flow.message.visible["text"]
    assert state.get_active_task(11) is None
    assert not state.get_user_lock(11).locked()
    assert not state.get_user_state(11).is_processing
    assert all(task.done() for task in flow.tasks)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in flow.inputs.values())
    assert not heartbeat._WAIT_NOTICE_TASKS
    assert not heartbeat._HEARTBEAT_OWNERS


async def test_real_stop_retry_joins_notice_before_recovery(request_flow):
    flow = request_flow
    await start_notice(flow)
    writes = len(flow.message.writes)
    callback, _query = flow.callback()
    await settle()
    assert flow.message.cleanup_entered.is_set(), "request cancellation left its notice running"
    assert not callback.done(), "recovery UI overtook owned notice cleanup"
    assert len(flow.message.writes) == writes
    flow.message.release_cleanup.set()
    await callback
    await settle()
    assert "retry_last" in buttons(flow.message.visible["reply_markup"])
    assert "retry_wait:1100" not in buttons(flow.message.visible["reply_markup"])
    assert state.get_active_task(11) is None
    assert not state.get_user_lock(11).locked()
    assert all(task.done() for task in flow.tasks)


async def test_stop_retry_does_not_write_recovery_for_superseding_edit(request_flow):
    flow = request_flow
    old = await start_notice(flow)
    callback, _query = flow.callback()
    await flow.message.cleanup_entered.wait()
    edit = flow.launch(edited=True)
    await settle()
    assert state.get_active_task(11) is edit
    flow.message.release_cleanup.set()
    await callback
    await old
    await edit
    await settle()
    assert not any(write["text"] == t("processing.long_wait_retry_prompt") for write in flow.message.writes), (
        "old stop/retry callback overwrote a superseding edit"
    )
    assert heartbeat.is_heartbeat_active(1100, chat_id=11)
    assert state.get_active_task(11) is not None
    flow.router.release.set()
    await state.get_active_task(11)


async def test_pending_notice_keeps_real_stop_retry_control(request_flow):
    flow = request_flow
    flow.message.gate_notice = False
    await start_notice(flow)
    await settle()
    assert buttons(flow.message.visible["reply_markup"]) == ["retry_wait:1100"]
    assert heartbeat.is_heartbeat_active(1100, chat_id=11)
    callback, _query = flow.callback()
    await callback
    assert buttons(flow.message.visible["reply_markup"]) == ["retry_last", "edit_query"]
    flow.router.release.set()
    await flow.launch()
    await settle()
    assert len(flow.router.requests) == 2, "full notice cleanup did not admit the next request"
    assert all(task.done() for task in flow.tasks)


async def test_user_cancel_while_notice_waits_never_starts_edit(request_flow):
    flow = request_flow
    await flow.launch()
    await flow.router.started.wait()
    await flow.delay_entered.wait()
    callback, _query = flow.callback()
    await callback
    flow.threshold.set()
    await settle()
    assert not flow.message.notice_entered.is_set()
    assert buttons(flow.message.visible["reply_markup"]) == ["retry_last", "edit_query"]
    assert all(task.done() for task in flow.tasks)


async def test_completion_before_notice_threshold_has_no_obsolete_notice(request_flow):
    flow = request_flow
    flow.router.release.set()
    await flow.launch()
    await settle()
    flow.threshold.set()
    await settle()
    assert "synthetic answer" in flow.message.visible["text"]
    assert not flow.message.notice_entered.is_set()
    assert all(task.done() for task in flow.tasks)


@pytest.mark.parametrize("cancel_target", ["observed_worker", "show_long_wait_retry"])
async def test_prestart_cancellation_closes_manager_inputs(request_flow, cancel_target):
    flow = request_flow
    flow.scheduling.cancel_before_start = cancel_target
    await flow.launch()
    await settle()
    if cancel_target == "show_long_wait_retry":
        assert flow.router.started.is_set()
        flow.router.release.set()
        await state.get_active_task(11)
    flow.threshold.set()
    await settle()
    assert not flow.message.notice_entered.is_set()
    assert state.get_active_task(11) is None
    assert not state.get_user_lock(11).locked()
    assert not state.get_user_state(11).is_processing
    assert not heartbeat.is_heartbeat_active(1100, chat_id=11)
    assert all(task.done() for task in flow.tasks)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in flow.inputs.values())


@pytest.mark.parametrize("error", [BadRequest("synthetic bad request"), NetworkError("synthetic network error")])
async def test_notice_transport_failure_is_best_effort(request_flow, error):
    flow = request_flow
    flow.message.notice_error = error
    await start_notice(flow)
    await settle()
    flow.router.release.set()
    await state.get_active_task(11)
    assert "synthetic answer" in flow.message.visible["text"]
    assert "tts_reply" in buttons(flow.message.visible["reply_markup"])
    assert all(task.done() for task in flow.tasks)


async def test_notice_cleanup_failure_still_allows_final_actions(request_flow):
    flow = request_flow
    flow.message.cleanup_error = True
    worker = await start_notice(flow)
    flow.router.release.set()
    await flow.message.cleanup_entered.wait()
    flow.message.release_cleanup.set()
    await worker
    assert "synthetic answer" in flow.message.visible["text"]
    assert "tts_reply" in buttons(flow.message.visible["reply_markup"])
    assert all(task.done() for task in flow.tasks)


async def test_repeated_worker_cancellation_still_joins_notice_cleanup(request_flow):
    flow = request_flow
    worker = await start_notice(flow)
    worker.cancel()
    await flow.message.cleanup_entered.wait()
    worker.cancel()
    await settle()
    assert not worker.done()
    assert state.get_active_task(11) is worker
    assert state.get_user_state(11).is_processing
    assert not flow.message.notice_finished.is_set()
    flow.message.release_cleanup.set()
    await asyncio.gather(worker, return_exceptions=True)
    await settle()
    assert state.get_active_task(11) is None
    assert all(task.done() for task in flow.tasks)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in flow.inputs.values())


async def test_completed_delivery_joins_the_same_notice_owner(request_flow):
    flow = request_flow
    worker = await start_notice(flow)
    writes = len(flow.message.writes)
    delivery = asyncio.create_task(
        flow.delivery.deliver(
            TelegramTarget(flow.message, chat_id=11),
            CompletedResponse("Completed synthetic answer"),
            presentation=FixedPresentation(),
        )
    )
    flow.owners.append(delivery)
    await flow.message.cleanup_entered.wait()
    await settle()
    assert len(flow.message.writes) == writes
    assert not delivery.done()
    flow.message.release_cleanup.set()
    await delivery
    assert flow.message.visible["text"] == "Completed synthetic answer"
    assert "retry_wait:1100" not in buttons(flow.message.visible.get("reply_markup"))
    flow.router.release.set()
    await worker


async def test_already_set_event_skips_notice_edit(request_flow):
    flow = request_flow
    done = asyncio.Event()
    done.set()
    await messages.show_long_wait_retry(flow.message, done)
    assert not flow.message.notice_entered.is_set()


async def test_edit_supersession_joins_notice_before_reusing_placeholder(request_flow):
    flow = request_flow
    old = await start_notice(flow)
    writes = len(flow.message.writes)
    edit = flow.launch(edited=True)
    await flow.message.cleanup_entered.wait()
    await settle()
    assert state.get_active_task(11) is edit
    assert not edit.done()
    assert len(flow.message.writes) == writes
    flow.message.release_cleanup.set()
    await old
    await edit
    await settle()
    replacement = state.get_active_task(11)
    assert replacement is not None and replacement is not old
    assert heartbeat.is_heartbeat_active(1100, chat_id=11)
    assert state.get_user_lock(11).locked()
    flow.router.release.set()
    await replacement
    assert "synthetic answer" in flow.message.visible["text"]
    assert "tts_reply" in buttons(flow.message.visible["reply_markup"])
    assert state.get_active_task(11) is None


async def test_retirement_caller_repeated_cancel_does_not_interrupt_child_cleanup(request_flow):
    flow = request_flow
    worker = await start_notice(flow)
    retirement = asyncio.create_task(heartbeat.retire_heartbeat(1100, chat_id=11))
    flow.owners.append(retirement)
    await flow.message.cleanup_entered.wait()
    retirement.cancel()
    await settle()
    retirement.cancel()
    await settle()
    assert not retirement.done()
    assert not flow.message.notice_finished.is_set()
    notice = next(task for task, coro in flow.inputs.items() if coro.__name__ == "show_long_wait_retry")
    assert notice.cancelling() == 1
    flow.message.release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await retirement
    assert flow.message.notice_finished.is_set()
    assert inspect.getcoroutinestate(flow.inputs[notice]) == inspect.CORO_CLOSED
    flow.router.release.set()
    await worker


async def test_stop_retry_timeout_leaves_cleanup_owned(request_flow, monkeypatch):
    flow = request_flow
    worker = await start_notice(flow)
    deadlines = []
    proxy = SimpleNamespace(**vars(asyncio))

    def timeout(seconds):
        assert seconds == 5.0
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    proxy.timeout = timeout
    monkeypatch.setattr(cb_ai_actions, "asyncio", proxy)
    callback, query = flow.callback()
    await flow.message.cleanup_entered.wait()
    writes = len(flow.message.writes)
    deadlines[0].reschedule(asyncio.get_running_loop().time())
    await callback
    assert query.answer.await_args.args == (t("processing.long_wait_stopping"),)
    assert len(flow.message.writes) == writes
    assert state.get_active_task(11) is worker
    assert state.get_user_state(11).is_processing
    assert not worker.done()
    flow.message.release_cleanup.set()
    await worker
    await settle()
    assert state.get_active_task(11) is None
    assert all(task.done() for task in flow.tasks)


async def test_equal_message_ids_in_two_chats_retire_only_matching_notice(request_flow):
    flow = request_flow
    first, second = flow.message, Message()
    second.chat_id = 22
    second.chat = SimpleNamespace(id=22, type="private", send_action=AsyncMock())
    first.chat.send_action = AsyncMock()
    events = [asyncio.Event(), asyncio.Event()]
    for message, event in zip((first, second), events, strict=True):
        messages.register_heartbeat(message.message_id, event, message.chat)
        coroutine = messages.show_long_wait_retry(message, event)
        task = messages.submit_task(coroutine)
        heartbeat.register_wait_notice(event, task, coroutine)
    flow.threshold.set()
    await first.notice_entered.wait()
    await second.notice_entered.wait()
    delivery = asyncio.create_task(
        flow.delivery.deliver(
            TelegramTarget(first, chat_id=11), CompletedResponse("First chat final"), presentation=FixedPresentation()
        )
    )
    flow.owners.append(delivery)
    try:
        await first.cleanup_entered.wait()
        await settle()
        assert not second.cleanup_entered.is_set()
        assert heartbeat.is_heartbeat_active(1100, chat_id=22)
        assert not events[1].is_set()
        first.release_cleanup.set()
        await delivery
        assert first.visible["text"] == "First chat final"
        assert not second.notice_finished.is_set()
    finally:
        second.release_cleanup.set()
        await heartbeat.retire_heartbeat(1100, chat_id=22)
        for event in events:
            heartbeat.unregister_heartbeat(1100, event=event)


async def test_edit_during_stop_toast_cannot_receive_old_recovery_ui(request_flow):
    flow = request_flow
    await start_notice(flow)
    callback, query = flow.callback()
    answer_entered, release_answer = asyncio.Event(), asyncio.Event()

    async def answer(*_args, **_kwargs):
        answer_entered.set()
        await release_answer.wait()

    query.answer.side_effect = answer
    await flow.message.cleanup_entered.wait()
    flow.message.release_cleanup.set()
    await answer_entered.wait()
    await flow.launch(edited=True)
    await settle()
    replacement = state.get_active_task(11)
    release_answer.set()
    await callback
    assert not any(write["text"] == t("processing.long_wait_retry_prompt") for write in flow.message.writes), (
        "stop toast await allowed obsolete recovery to overwrite a newer edit"
    )
    flow.router.release.set()
    await replacement


async def test_agent_protocol_error_fallback_joins_notice_before_recovery_write(request_flow):
    """The real agent fallback must not publish recovery before notice retirement."""
    flow = request_flow
    flow.router.events = []
    worker = await start_notice(flow)
    writes = len(flow.message.writes)
    flow.router.release.set()
    await flow.message.cleanup_entered.wait()
    await settle()
    assert len(flow.message.writes) == writes, "agent fallback overtook owned notice cleanup"
    assert not worker.done()
    assert state.get_active_task(11) is worker
    flow.message.release_cleanup.set()
    await worker
    await settle()
    assert flow.router.closed.is_set()
    assert flow.message.notice_finished.is_set()
    assert "Произошла ошибка" in flow.message.visible["text"]
    assert buttons(flow.message.visible["reply_markup"]) == ["retry_last", "edit_query", "open_roles:from_response"]
    assert "retry_wait:1100" not in buttons(flow.message.visible["reply_markup"])
    assert state.get_active_task(11) is None
    assert not state.get_user_lock(11).locked()
    assert not state.get_user_state(11).is_processing
    assert all(task.done() for task in flow.tasks)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in flow.inputs.values())
    assert not heartbeat._WAIT_NOTICE_TASKS
    assert not heartbeat._HEARTBEAT_OWNERS


async def start_notice_at_boundary(flow, boundary_entered, *, text="hello"):
    await flow.launch(text=text)
    await boundary_entered.wait()
    worker = state.get_active_task(11)
    assert worker is not None and not worker.done()
    flow.threshold.set()
    await flow.message.notice_entered.wait()
    return worker


async def release_direct_output_after_notice(flow, worker, release_boundary):
    writes = len(flow.message.writes)
    release_boundary.set()
    await settle()
    assert flow.message.cleanup_entered.is_set(), "direct UI boundary did not begin owned notice retirement"
    assert len(flow.message.writes) == writes, "direct UI write overtook owned notice cleanup"
    assert not worker.done()
    assert state.get_active_task(11) is worker
    flow.message.release_cleanup.set()


async def assert_request_drained(flow, worker):
    await worker
    await settle()
    assert flow.message.notice_finished.is_set()
    assert "retry_wait:1100" not in buttons(flow.message.visible.get("reply_markup"))
    assert state.get_active_task(11) is None
    assert not state.get_user_lock(11).locked()
    assert not state.get_user_state(11).is_processing
    assert all(task.done() for task in flow.tasks)
    assert all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in flow.inputs.values())
    assert not heartbeat._WAIT_NOTICE_TASKS
    assert not heartbeat._HEARTBEAT_OWNERS


@pytest.mark.parametrize(
    "refusal",
    ["blocked", "generation", "lyria_lease", "all_exhausted", "decryption_failed", "confirm_fallback", "chat_lease"],
)
async def test_real_chat_preflight_direct_output_joins_notice(request_flow, monkeypatch, refusal):
    """Each early decision must retain its policy while retiring the competing notice."""
    flow = request_flow
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(value):
        entered.set()
        await release.wait()
        return value

    if refusal == "blocked":
        flow.chat_state.private_data_blocked = True
        monkeypatch.setattr(agent, "get_user_chat", lambda _uid: gated(flow.chat_state))
    elif refusal == "generation":
        monkeypatch.setattr(ai_chat, "ensure_chat_generation", lambda *args, **kwargs: gated(None))
    elif refusal in {"all_exhausted", "decryption_failed", "confirm_fallback"}:
        monkeypatch.setattr(ai_chat, "_resolve_ai_request", lambda *_args: gated((None, "gemini-fallback", refusal)))
    else:
        if refusal == "lyria_lease":
            flow.chat_state.model = "or/google/lyria-3-clip-preview"

        @asynccontextmanager
        async def refused_lease(*args, **kwargs):
            assert kwargs["purpose"] == ("conversation:lyria" if refusal == "lyria_lease" else "conversation:chat")
            yield await gated(False)

        monkeypatch.setattr("app.repos.memory_consent.private_data_lease", refused_lease)
    worker = await start_notice_at_boundary(flow, entered)
    await release_direct_output_after_notice(flow, worker, release)
    await assert_request_drained(flow, worker)
    assert not flow.router.requests, "refused private/configuration work reached the provider"
    visible = flow.message.visible
    if refusal == "confirm_fallback":
        assert "gemini-fallback" in visible["text"]
        assert buttons(visible["reply_markup"]) == ["fallback:confirm:gemini-fallback", "fallback:cancel"]
    elif refusal == "all_exhausted":
        assert "Все лимиты" in visible["text"]
    elif refusal == "decryption_failed":
        assert "безопасное подключение" in visible["text"]
    else:
        assert "error occurred" in visible["text"].lower()
    if refusal == "chat_lease":
        assert flow.chat_state.history == []


@pytest.mark.parametrize("boundary", ["research_generation", "research_lease", "research_stop", "qna_stop"])
async def test_real_search_direct_handoff_joins_notice(request_flow, monkeypatch, boundary):
    flow = request_flow
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(ai_search, "ensure_chat_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(ai_search.metrics_collector, "record_search_query", AsyncMock())

    async def gated(value):
        entered.set()
        await release.wait()
        return value

    if boundary == "research_generation":
        monkeypatch.setattr(ai_search, "ensure_chat_generation", lambda *args, **kwargs: gated(None))
    else:

        @asynccontextmanager
        async def gated_lease(*args, **kwargs):
            yield await gated(boundary != "research_lease")

        monkeypatch.setattr("app.repos.memory_consent.private_data_lease", gated_lease)
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_search, "get_primary_provider", lambda: "gemini")
    text = "?synthetic question" if boundary == "qna_stop" else "??synthetic research"
    worker = await start_notice_at_boundary(flow, entered, text=text)
    await release_direct_output_after_notice(flow, worker, release)
    if boundary == "qna_stop":
        await flow.router.started.wait()
        flow.router.release.set()
    await assert_request_drained(flow, worker)
    if boundary in {"research_generation", "research_lease"}:
        assert flow.message.visible["text"] == "Запрос отменён: настройки приватности изменились."
        assert not flow.router.requests
    elif boundary == "research_stop":
        assert "Сервис ответов временно недоступен" in flow.message.visible["text"]
        assert buttons(flow.message.visible["reply_markup"]) == ["retry_last", "edit_query", "open_roles:from_response"]
    else:
        assert "synthetic answer" in flow.message.visible["text"]
        assert buttons(flow.message.visible["reply_markup"]) == ["open_roles:from_response", "new_topic"]


@pytest.mark.parametrize("terminal", ["audio", "send_failure", "text", "error"])
async def test_real_lyria_terminal_joins_notice_after_generation(request_flow, monkeypatch, terminal):
    from app.providers.freetheai_audio import AudioGenResult

    flow = request_flow
    flow.chat_state.model = "or/google/lyria-3-clip-preview"
    entered, release = asyncio.Event(), asyncio.Event()
    audio_sends, deleted = [], []

    async def generate(*, prompt, model):
        assert prompt == "hello" and model == "or/google/lyria-3-clip-preview"
        entered.set()
        await release.wait()
        if terminal in {"audio", "send_failure"}:
            return AudioGenResult(success=True, audio_bytes=b"synthetic-audio")
        if terminal == "text":
            return AudioGenResult(success=True, text_content="Synthetic lyrics")
        return AudioGenResult(success=False, error_message="no_keys")

    async def reply_audio(**kwargs):
        audio_sends.append(kwargs)
        if terminal == "send_failure":
            raise NetworkError("synthetic Telegram audio send failure")
        assert kwargs["audio"].getvalue() == b"synthetic-audio"
        flow.message.visible = {"text": "audio delivered", **kwargs}
        flow.message.writes.append(flow.message.visible)

    async def delete():
        deleted.append(True)

    monkeypatch.setattr("app.providers.freetheai_audio.get_lyria_provider", lambda: SimpleNamespace(generate=generate))
    flow.message.reply_audio = reply_audio
    flow.message.delete = delete
    worker = await start_notice_at_boundary(flow, entered)
    # Preserve the existing policy: progress/generation leaves stop/retry available.
    assert heartbeat.is_heartbeat_active(1100, chat_id=11)
    assert not audio_sends
    await release_direct_output_after_notice(flow, worker, release)
    assert not audio_sends, "audio publication overtook notice cleanup"
    await assert_request_drained(flow, worker)
    if terminal == "audio":
        assert flow.message.visible["text"] == "audio delivered"
        assert buttons(flow.message.visible["reply_markup"]) == ["retry_last"]
        assert deleted == [True]
    elif terminal == "send_failure":
        assert "Аудио создано, но не удалось отправить" in flow.message.visible["text"]
        assert len(audio_sends) == 1 and not deleted
    elif terminal == "text":
        assert "Synthetic lyrics" in flow.message.visible["text"]
        assert buttons(flow.message.visible["reply_markup"]) == ["retry_last"]
    else:
        assert "Нет доступных ключей FreeTheAI" in flow.message.visible["text"]
        assert buttons(flow.message.visible["reply_markup"]) == ["retry_last"]
