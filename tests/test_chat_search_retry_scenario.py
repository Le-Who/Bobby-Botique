"""Offline consumer → delivery → recovery callback → consumer scenarios."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.errors import ErrorCode
from app.handlers import ai_chat, ai_search, cb_ai_actions
from app.handlers.retry_requests import current_request
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GroundingReport,
    KeyDisposition,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TokenUsage,
)
from app.response_delivery.delivery import TelegramResponseDelivery
from app.response_delivery.renderer import DeliveryKind, DeliveryReceipt, TelegramMessageRef
from app.runtime_settings import lifecycle
from app.runtime_settings.store import SettingsSnapshot
from tests.factories import make_chat_state

MODEL = "openai-direct/gpt-6-luna"
USER_ID = 910042
TEXT = "Объясни коротко, как организовать спокойный вечер после работы."


def failure(phase=FailurePhase.BEFORE_TEXT, code=ErrorCode.TIMEOUT):
    return StreamFailed(code, phase, RetryDisposition.DO_NOT_RETRY, KeyDisposition.UNCHANGED, "synthetic failure")


def completion():
    return StreamCompleted(
        FinishReason.from_raw("STOP"),
        TokenUsage(total=20),
        GroundingReport(),
        RouteUsed(ProviderKind.OPENAI, MODEL, MODEL),
    )


@pytest.fixture
async def scenario(monkeypatch):
    state.purge_user_runtime_state(USER_ID)
    user_state = state.get_user_state(USER_ID)
    user_state._loaded_from_db = True
    chat = make_chat_state(model="gemini-previous-user-choice", ltm_enabled=True, memory_epoch=9)
    chat.voice_id = None
    chat.tts_temperature = None
    scripts, requests, closed, sessions = [], [], [], []
    tasks = set()

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    async def generation(uid, *, expected_epoch):
        return expected_epoch

    class Router:
        async def stream(self, request):
            requests.append(request)
            script = scripts.pop(0)
            try:
                for event in script:
                    if isinstance(event, asyncio.Event):
                        await event.wait()
                    else:
                        yield event
            finally:
                closed.append(request)

    class Session:
        def __init__(self, ref):
            self.ref, self.finalizations, self.text = ref, [], ""

        async def append(self, text):
            self.text += text

        async def show_status(self, text, actions=None):
            pass

        async def finalize(self, **kwargs):
            self.finalizations.append(kwargs)
            return DeliveryReceipt(DeliveryKind.MESSAGE, (self.ref.message_id,), self.ref)

    def renderer(transport, **kwargs):
        session = Session(transport.current_ref)
        sessions.append(session)
        return SimpleNamespace(open=lambda: session)

    delivery = TelegramResponseDelivery(Router(), renderer_factory=renderer)
    serial = [100]

    def message():
        value = MagicMock()
        value.message_id = serial[0]
        serial[0] += 1
        value.chat_id = value.chat.id = USER_ID
        value.chat.type = "private"
        value.reply_to_message = None
        value.get_bot.return_value = None
        value.edit_text = AsyncMock()
        value.edit_reply_markup = AsyncMock()
        value.reply_text = AsyncMock(side_effect=lambda *args, **kwargs: message())
        return value

    async def save(uid, value, **kwargs):
        return True

    saved = AsyncMock(side_effect=save)
    memory = MagicMock()
    grounding = AsyncMock(return_value="Synthetic supplied search evidence")
    monkeypatch.setattr(state, "_schedule_persist", lambda value: None)
    monkeypatch.setattr(state, "ensure_state_loaded", AsyncMock(return_value=user_state))
    monkeypatch.setattr(ai_chat, "ensure_chat_generation", generation)
    monkeypatch.setattr(ai_search, "ensure_chat_generation", generation)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-embedding-key", "key_hash": "embedding-hash"}),
    )
    monkeypatch.setattr(
        "app.context.compression.inject_memory_layers",
        AsyncMock(side_effect=lambda **kwargs: (kwargs["system_instruction"], {})),
    )
    monkeypatch.setattr(ai_chat, "update_stage", AsyncMock())
    monkeypatch.setattr(ai_search, "update_stage", AsyncMock())
    monkeypatch.setattr(ai_chat, "update_user_chat", saved)
    monkeypatch.setattr(ai_chat, "_store_memory_in_background", memory)
    monkeypatch.setattr(
        ai_chat, "_resolve_ai_request", AsyncMock(side_effect=AssertionError("legacy Gemini preflight"))
    )
    monkeypatch.setattr(
        "app.agent_use_cases.AgentRequestUseCase.resolve_exact_ai_request",
        AsyncMock(return_value=({"api_key": "test-openai-key", "key_hash": "hash"}, MODEL, "direct")),
    )
    monkeypatch.setattr("app.search_jina.search_for_grounding", grounding)
    monkeypatch.setattr("app.response_delivery.delivery.get_telegram_response_delivery", lambda: delivery)
    monkeypatch.setattr(cb_ai_actions, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cb_ai_actions, "_background_tasks", tasks)
    monkeypatch.setattr(cb_ai_actions, "_HEAVY_CALLBACK_SEMAPHORE", asyncio.Semaphore(1))
    snapshot = SettingsSnapshot(
        101,
        {
            "live_route:provider": "openai",
            **{
                f"process:{name}": {"models": [MODEL], "strategy": "sequential", "inherit_user_model": False}
                for name in ("chat", "search")
            },
        },
    )
    token = lifecycle._operation_snapshot.set(snapshot)

    async def click(source, *, callback=None, wait=True):
        request = current_request(USER_ID)
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=USER_ID),
            id="synthetic-callback",
            message=source,
            data=callback or request.callback_data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await cb_ai_actions.retry_last_callback(SimpleNamespace(callback_query=query), SimpleNamespace())
        if wait:
            await asyncio.gather(*tuple(tasks))
        return query

    value = SimpleNamespace(
        chat=chat,
        scripts=scripts,
        requests=requests,
        closed=closed,
        sessions=sessions,
        tasks=tasks,
        message=message,
        saved=saved,
        memory=memory,
        grounding=grounding,
        click=click,
    )
    try:
        yield value
    finally:
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
        lifecycle._operation_snapshot.reset(token)
        state.purge_user_runtime_state(USER_ID)


def retry_button(session):
    actions = session.finalizations[0]["actions"]
    return next(
        button.callback_data
        for row in actions.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("retry_last")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [ErrorCode.TIMEOUT, ErrorCode.RATE_LIMIT, ErrorCode.OVERLOADED])
async def test_chat_error_button_replays_once_without_duplicate_history_or_memory(scenario, code):
    s = scenario
    s.scripts.extend([[failure(code=code)], [TextDelta("A complete replacement answer"), completion()]])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    assert s.chat.history == []
    assert s.memory.call_count == 0
    button = retry_button(s.sessions[0])
    assert button == current_request(USER_ID).callback_data
    await s.click(original, callback=button)
    assert s.chat.history == [
        {"role": "user", "parts": [TEXT]},
        {"role": "model", "parts": ["A complete replacement answer"]},
    ]
    assert s.memory.call_count == 1
    assert len(s.requests) == len(s.closed) == 2
    assert all(request.models == (MODEL,) and not request.allow_model_fallback for request in s.requests)
    assert all(len(session.finalizations) == 1 for session in s.sessions)
    assert s.chat.model == "gemini-previous-user-choice"
    assert not state.get_user_lock(USER_ID).locked()
    original.edit_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_timeout_retry_uses_same_effective_query_and_never_chat(scenario, monkeypatch):
    s = scenario
    s.grounding.side_effect = AssertionError("Direct OpenAI search must not call JINA, including on retry")
    s.scripts.extend([[failure()], [TextDelta("Grounded retry answer"), completion()]])
    original = s.message()
    await ai_search._handle_qna_search(
        original, "Исходный объединённый вопрос", s.chat, search_query="точный поисковый запрос", user_id=USER_ID
    )
    chat = AsyncMock(side_effect=AssertionError("Search retry must not enter chat"))
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", chat)
    await s.click(original, callback=retry_button(s.sessions[0]))
    assert len(s.requests) == len(s.closed) == 2
    s.grounding.assert_not_awaited()
    assert all(request.grounding.value == "provider_search_required" for request in s.requests)
    assert all(request.turns[0].parts[0].text == "точный поисковый запрос" for request in s.requests)
    assert s.chat.history == []
    assert s.memory.call_count == 0
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_chat_retry_failure_keeps_partial_then_success_replaces_one_turn(scenario):
    s = scenario
    s.scripts.extend(
        [
            [TextDelta("Original partial answer"), failure(FailurePhase.AFTER_TEXT)],
            [failure()],
            [TextDelta("New complete answer"), completion()],
        ]
    )
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    prior = list(s.chat.history)
    assert prior[-1]["parts"] == ["Original partial answer"]
    assert s.memory.call_count == 1
    await s.click(original)
    assert s.chat.history == prior
    assert s.memory.call_count == 1
    assert all("Original partial answer" not in part.text for turn in s.requests[1].turns for part in turn.parts)
    failed_message = s.message()
    failed_message.message_id = current_request(USER_ID).response_ref[0]
    await s.click(failed_message)
    assert s.chat.history == [{"role": "user", "parts": [TEXT]}, {"role": "model", "parts": ["New complete answer"]}]
    assert s.memory.call_count == 1
    assert s.saved.await_args.kwargs["rewrite_history"] is True


@pytest.mark.asyncio
async def test_double_click_is_busy_and_old_response_becomes_stale(scenario):
    s = scenario
    gate = asyncio.Event()
    s.scripts.extend([[failure()], [gate, TextDelta("Finished answer"), completion()]])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    first = await s.click(original, wait=False)
    second = await s.click(original, wait=False)
    assert "⏳" in second.answer.await_args.args[0]
    assert original.reply_text.await_count == 1
    gate.set()
    await asyncio.gather(*tuple(s.tasks))
    stale = await s.click(original, callback=first.data)
    assert stale.answer.await_args.kwargs["show_alert"] is True
    assert len(s.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["generation", "branch"])
async def test_retry_rejects_changed_privacy_or_branch_without_calling_provider(scenario, changed):
    s = scenario
    s.scripts.append([failure()])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    if changed == "generation":
        s.chat.memory_epoch += 1
    else:
        s.chat.branch_id = "new-branch"
    query = await s.click(original)
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert len(s.requests) == 1
    original.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_search_retry_keeps_prefix_routing(scenario):
    s = scenario
    state.get_user_state(USER_ID).last_sent_message_text = "? legacy exact query"
    s.scripts.append([TextDelta("Legacy grounded answer"), completion()])
    await s.click(s.message(), callback="retry_last")
    s.grounding.assert_not_awaited()
    assert s.requests[0].grounding.value == "provider_search_required"
    assert s.requests[0].turns[0].parts[0].text == "legacy exact query"
    assert s.chat.history == []


@pytest.mark.asyncio
async def test_partial_search_retains_text_and_retries_same_query(scenario):
    s = scenario
    s.scripts.extend(
        [
            [TextDelta("Visible partial search result"), failure(FailurePhase.AFTER_TEXT)],
            [TextDelta("Complete grounded result"), completion()],
        ]
    )
    original = s.message()
    result = await ai_search._handle_qna_search(
        original, "Original search text", s.chat, search_query="effective query", user_id=USER_ID
    )
    assert result == "Visible partial search result"
    assert "Visible partial search result" in s.sessions[0].finalizations[0]["displayed_text"]
    await s.click(original, callback=retry_button(s.sessions[0]))
    assert [request.turns[0].parts[0].text for request in s.requests] == ["effective query"] * 2
    assert s.chat.history == []
    assert s.memory.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("before_start", [True, False])
async def test_retry_cancellation_drains_owner_and_preserves_original_history(scenario, before_start):
    s = scenario
    gate = asyncio.Event()
    s.scripts.extend([[failure()], [gate, TextDelta("Never published"), completion()]])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    await s.click(original, wait=False)
    task = state.get_active_task(USER_ID)
    assert task is not None
    if not before_start:
        async with asyncio.timeout(1):
            while len(s.requests) < 2:
                await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not state.get_user_lock(USER_ID).locked()
    assert state.get_active_task(USER_ID) is None
    assert current_request(USER_ID).running is False
    assert len(s.closed) == len(s.requests)
    assert s.chat.history == []
    assert s.memory.call_count == 0


@pytest.mark.asyncio
async def test_retry_preserves_capture_opt_out_and_forward_context(scenario):
    s = scenario
    s.scripts.extend([[failure()], [TextDelta("Forward analysis"), completion()]])
    original = s.message()
    await ai_chat._handle_regular_chat(
        original,
        USER_ID,
        TEXT,
        s.chat,
        capture_text_memory=False,
        is_forward_batch=True,
        user_parts=["[Voice transcript]\n" + TEXT],
    )
    await s.click(original)
    assert s.memory.call_count == 0
    assert "forward_analysis_directive" in s.requests[1].system_instruction
    assert s.chat.history[0]["parts"] == ["[Voice transcript]\n" + TEXT]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["new_topic", "erasure"])
async def test_reset_clears_retry_and_legacy_text_even_without_changed_generation(scenario, monkeypatch, reset):
    from app.handlers import cb_navigation

    s = scenario
    s.scripts.append([failure()])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    callback = retry_button(s.sessions[0])
    prior_user_state = state.get_user_state(USER_ID)
    prior_user_state.last_sent_message_text = TEXT
    assert s.chat.history == []  # A length check alone cannot detect this reset.
    if reset == "new_topic":
        monkeypatch.setattr(cb_navigation, "get_user_chat", AsyncMock(return_value=s.chat))
        monkeypatch.setattr(cb_navigation, "update_user_chat", AsyncMock(return_value=True))
        monkeypatch.setattr(cb_navigation, "leave_tarot_for_chat", AsyncMock())
        monkeypatch.setattr(cb_navigation, "_is_user_busy", lambda uid: False)
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=USER_ID),
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            message=original,
        )
        await cb_navigation.new_topic_callback(
            SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER_ID, language_code="ru")),
            SimpleNamespace(),
        )
    else:
        state.purge_user_runtime_state(USER_ID)
    assert current_request(USER_ID) is None
    assert state.get_last_sent_message(USER_ID) is None
    assert prior_user_state.retry_request is None
    assert prior_user_state.last_sent_message_text is None
    rejected = await s.click(original, callback=callback)
    assert rejected.answer.await_args.kwargs["show_alert"] is True
    assert len(s.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["new_topic", "new_chat", "deep_topic", "branch_create", "branch_return", "new_chat_command"]
)
async def test_reset_reserves_user_before_hydration_so_retry_cannot_overtake(scenario, monkeypatch, action):
    from app.handlers import cb_branches, cb_navigation, commands

    s = scenario
    s.scripts.extend([[failure()], [TextDelta("Must never be requested"), completion()]])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    old_callback = current_request(USER_ID).callback_data
    entered, release = asyncio.Event(), asyncio.Event()

    async def hydrate(uid):
        entered.set()
        await release.wait()
        return s.chat

    module = (
        commands if action == "new_chat_command" else cb_branches if action.startswith("branch_") else cb_navigation
    )
    monkeypatch.setattr(module, "get_user_chat", hydrate)
    monkeypatch.setattr(module, "update_user_chat", AsyncMock(return_value=True))
    monkeypatch.setattr(cb_navigation, "leave_tarot_for_chat", AsyncMock())
    monkeypatch.setattr(commands, "leave_tarot_for_chat", AsyncMock())
    monkeypatch.setattr(cb_branches, "create_branch", AsyncMock(return_value=23))
    monkeypatch.setattr(cb_branches, "restore_branch", AsyncMock(return_value=[]))
    monkeypatch.setattr(cb_branches, "delete_branch", AsyncMock())
    if action == "branch_return":
        s.chat.branch_id = 23
        current_request(USER_ID).branch_id = 23
    query = SimpleNamespace(
        data="deepdive:new_topic",
        from_user=SimpleNamespace(id=USER_ID),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=original,
    )
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=USER_ID, language_code="ru"), message=original
    )
    callback = {
        "new_topic": cb_navigation.new_topic_callback,
        "new_chat": cb_navigation.new_chat_callback,
        "deep_topic": cb_navigation.deep_dive_callback,
        "branch_create": cb_branches.branch_create_callback,
        "branch_return": cb_branches.branch_return_callback,
        "new_chat_command": commands.new_chat_command.__wrapped__,
    }[action]
    worker = asyncio.create_task(callback(update, SimpleNamespace(user_data={})))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rejected = await s.click(original, callback=old_callback)
        assert "⏳" in rejected.answer.await_args.args[0]
        assert len(s.requests) == 1
    finally:
        release.set()
        await worker
    assert current_request(USER_ID) is None
    assert not state.get_user_lock(USER_ID).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["complete", "failure", "cancel"])
async def test_invalidated_retry_cannot_write_or_roll_back_reset_history(scenario, terminal):
    s = scenario
    gate = asyncio.Event()
    tail = [TextDelta("Late replacement"), completion()] if terminal == "complete" else [failure()]
    s.scripts.extend([[failure()], [gate, *tail]])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    await s.click(original, wait=False)
    async with asyncio.timeout(2):
        while len(s.requests) < 2:
            await asyncio.sleep(0)
    # An erasure/reset path outside the callbacks may invalidate a running request.
    state.clear_retry_request(USER_ID)
    s.chat.history = []
    s.chat.context_summary = "Reset summary"
    previous_writes = s.saved.await_count
    if terminal == "cancel":
        state.get_active_task(USER_ID).cancel()
    gate.set()
    await asyncio.gather(*tuple(s.tasks), return_exceptions=True)
    assert s.saved.await_count == previous_writes
    assert s.chat.history == []
    assert s.chat.context_summary == "Reset summary"
    assert s.memory.call_count == 0


@pytest.mark.asyncio
async def test_initial_cancel_after_compaction_retries_the_retained_user_once(scenario, monkeypatch):
    from app.context_assembler import get_assembler

    s = scenario
    original_history = [{"role": "user", "parts": ["Old question"]}, {"role": "model", "parts": ["Old answer"]}]
    s.chat.history = original_history
    assembler = get_assembler()
    assemble = assembler.assemble
    assembly_calls = []

    def compact_once(**kwargs):
        assembly_calls.append(list(kwargs["history"]))
        if len(assembly_calls) == 1:
            kwargs["history"] = []
            kwargs["existing_summary"] = "Compacted summary"
        result = assemble(**kwargs)
        if len(assembly_calls) == 1:
            result.was_truncated = True
            result.messages_dropped = 2
            result.dropped_messages = original_history
        return result

    monkeypatch.setattr(assembler, "assemble", compact_once)
    monkeypatch.setattr("app.metrics.role_conv_metrics.record_summarization", AsyncMock())
    gate = asyncio.Event()
    s.scripts.extend([[TextDelta("Interrupted initial answer"), gate], [TextDelta("Complete answer"), completion()]])
    original = s.message()
    worker = asyncio.create_task(ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat))
    async with asyncio.timeout(2):
        while not s.sessions or not s.sessions[0].text:
            await asyncio.sleep(0)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2)
    assert s.chat.history == [{"role": "user", "parts": [TEXT]}]
    assert current_request(USER_ID).history_start == 0
    s.saved.assert_not_awaited()
    await asyncio.wait_for(s.click(original, callback="retry_last"), 2)
    assert assembly_calls[1] == []
    assert s.chat.history == [{"role": "user", "parts": [TEXT]}, {"role": "model", "parts": ["Complete answer"]}]
    assert s.saved.await_args.kwargs["rewrite_history"] is True
    assert s.memory.call_count == 1


@pytest.mark.asyncio
async def test_retry_persistence_error_after_delivery_preserves_final_answer_actions(scenario):
    s = scenario
    s.scripts.extend([[failure()], [TextDelta("Delivered answer stays visible"), completion()]])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    retry_placeholder = s.message()
    original.reply_text = AsyncMock(return_value=retry_placeholder)
    s.saved.side_effect = RuntimeError("synthetic persistence failure")
    await s.click(original)
    assert s.sessions[1].text == "Delivered answer stays visible"
    assert len(s.sessions[1].finalizations) == 1
    assert retry_button(s.sessions[1]) == current_request(USER_ID).callback_data
    assert current_request(USER_ID).response_ref == (retry_placeholder.message_id, USER_ID)
    retry_placeholder.edit_text.assert_not_awaited()
    retry_placeholder.edit_reply_markup.assert_not_awaited()
    assert not state.get_user_lock(USER_ID).locked()


@pytest.mark.asyncio
async def test_retry_error_before_delivery_keeps_error_and_retry_actions(scenario, monkeypatch):
    from app.handlers import agent

    s = scenario
    s.scripts.append([failure()])
    original = s.message()
    await ai_chat._handle_regular_chat(original, USER_ID, TEXT, s.chat)
    retry_placeholder = s.message()
    original.reply_text = AsyncMock(return_value=retry_placeholder)
    monkeypatch.setattr(agent, "_handle_regular_chat", AsyncMock(side_effect=RuntimeError("synthetic setup failure")))
    await s.click(original)
    retry_placeholder.edit_text.assert_awaited_once()
    actions = retry_placeholder.edit_text.await_args.kwargs["reply_markup"]
    assert actions.inline_keyboard[0][0].callback_data == current_request(USER_ID).callback_data
    assert current_request(USER_ID).response_ref == (retry_placeholder.message_id, USER_ID)
    assert len(s.sessions) == 1
    assert not state.get_user_lock(USER_ID).locked()
