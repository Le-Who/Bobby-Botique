"""Admitted callback jobs own one user operation; UI and cancellation own none."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import metrics
from app.metrics import MetricsCollector
from tests import test_voice_callback_lifecycle as voice_support

voice_case = voice_support.voice_case


@pytest.fixture
def collector(monkeypatch):
    value = MetricsCollector()
    monkeypatch.setattr(metrics, "metrics_collector", value)
    return value


def assert_user_counts(collector, user_id, requests, errors=0):
    collector._drain_events()
    assert collector.metrics.request_count == requests
    assert collector.metrics.error_count == errors
    assert sum(users.get(user_id, {}).get("request_count", 0) for users in collector._user_daily.values()) == requests


async def fail_or_complete(outcome):
    if outcome == "failed":
        raise ValueError("synthetic callback failure")
    if outcome == "cancelled":
        raise asyncio.CancelledError
    return outcome != "handled_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["execute", "regen"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "handled_failure", "cancelled"])
async def test_draw_execution_owns_one_completed_operation(monkeypatch, collector, action, outcome):
    from app.handlers import cb_image, cmd_image

    user = SimpleNamespace(id=42)
    query = SimpleNamespace(from_user=user, data=f"draw:{action}", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=user)
    context = SimpleNamespace(user_data={"draw_state": {"prompt": "Synthetic image", "model": "flux"}})
    tasks = []
    monkeypatch.setattr("app.utils.background_tasks.submit_task", lambda coro: tasks.append(asyncio.create_task(coro)))

    async def generate(**kwargs):
        return await fail_or_complete(outcome)

    monkeypatch.setattr(cmd_image, "_run_generation", generate)
    await cb_image.draw_callback(update, context)
    await asyncio.gather(*tasks, return_exceptions=True)
    requests = int(outcome != "cancelled")
    assert_user_counts(collector, 42, requests, int(outcome in {"failed", "handled_failure"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", ["busy", "empty", "menu"])
async def test_draw_rejection_or_navigation_does_not_count_a_request(monkeypatch, collector, rejected):
    from app import state
    from app.handlers import cb_image

    user = SimpleNamespace(id=42)
    query = SimpleNamespace(
        from_user=user,
        data="draw:nav:main" if rejected == "menu" else "draw:execute",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    context = SimpleNamespace(user_data={"draw_state": {"prompt": "" if rejected == "empty" else "Synthetic image"}})
    monkeypatch.setattr(cb_image, "_main_keyboard", lambda value: None)
    if rejected == "busy":
        state.get_user_state(42).is_processing = True
    await cb_image.draw_callback(SimpleNamespace(callback_query=query), context)
    assert_user_counts(collector, 42, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "vision_only"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_complex_search_callback_owns_the_admitted_job(monkeypatch, collector, action, outcome):
    from app.handlers import cb_ai_actions

    user = SimpleNamespace(id=42)
    original = SimpleNamespace(from_user=user, caption="? synthetic image")
    message = SimpleNamespace(reply_to_message=original, edit_text=AsyncMock(), chat=SimpleNamespace(id=42))
    query = SimpleNamespace(
        from_user=user,
        id="synthetic",
        data=f"complex_search:{action}",
        message=message,
        answer=AsyncMock(),
    )
    tasks = set()
    monkeypatch.setattr(cb_ai_actions, "_background_tasks", tasks)
    monkeypatch.setattr(cb_ai_actions, "get_user_chat", AsyncMock(return_value=SimpleNamespace(history=[])))
    monkeypatch.setattr("app.adapters.concurrency.ultra_heavy_semaphore", asyncio.Semaphore(1))

    async def work(*args, **kwargs):
        await fail_or_complete(outcome)

    monkeypatch.setattr(cb_ai_actions.agent, "_handle_complex_agent_search", work)
    monkeypatch.setattr(cb_ai_actions.agent, "_handle_photo", work)
    await cb_ai_actions.complex_search_callback(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={}))
    await asyncio.gather(*tuple(tasks), return_exceptions=True)
    assert_user_counts(collector, 42, int(outcome != "cancelled"), int(outcome == "failed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("has_source", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_smart_research_counts_only_when_source_is_admitted(monkeypatch, collector, has_source, outcome):
    from app.handlers import ai_search, cb_smart_actions

    history = [{"role": "user", "parts": ["Synthetic research"]}] if has_source else []
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=SimpleNamespace(history=history)))

    async def work(*args, **kwargs):
        await fail_or_complete(outcome)

    monkeypatch.setattr(ai_search, "_handle_research_agent", work)
    message = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace()))
    if has_source and outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await cb_smart_actions._route_research(message, 42)
    else:
        await cb_smart_actions._route_research(message, 42)
    assert_user_counts(
        collector, 42, int(has_source and outcome != "cancelled"), int(has_source and outcome == "failed")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "deep_search"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_voice_ai_callback_owns_its_admitted_job(collector, voice_case, action, outcome):
    from app.handlers import cb_voice

    voice_case.query.data = f"voice:{action}"

    async def work(*args, **kwargs):
        await fail_or_complete(outcome)

    voice_case.dispatch.side_effect = work
    await cb_voice.voice_callback(voice_case.update, voice_case.context)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await voice_support.run_tasks(voice_case)
    else:
        await voice_support.run_tasks(voice_case)
    assert_user_counts(collector, 101, int(outcome != "cancelled"), int(outcome == "failed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", ["busy", "missing", "stale"])
async def test_voice_confirm_rejection_does_not_count_an_operation(collector, voice_case, rejected):
    from app.handlers import cb_voice

    voice_case.query.data = "voice:confirm"
    if rejected == "busy":
        await voice_case.lock.acquire()
    elif rejected == "missing":
        voice_case.context.user_data.pop("voice_pending_10")
    else:
        voice_case.current.return_value = False
    try:
        await cb_voice.voice_callback(voice_case.update, voice_case.context)
        await voice_support.run_tasks(voice_case)
    finally:
        if rejected == "busy":
            voice_case.lock.release()
    assert_user_counts(collector, 101, 0)


@pytest.mark.asyncio
async def test_user_scope_preserves_nested_failure_and_resets_it_after_exit(collector):
    from app.utils import metrics_middleware

    async with metrics_middleware.MetricsMiddleware("callback", user_id=42):
        metrics_middleware.mark_operation_failed()
        async with metrics_middleware.MetricsMiddleware("internal"):
            pass
        assert metrics_middleware.operation_failed() is True
    assert metrics_middleware.operation_failed() is False
    assert_user_counts(collector, 42, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["fallback", "retry_last", "continue_stream"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_chat_recovery_callback_owns_its_job(monkeypatch, collector, callback, outcome):
    from app import state
    from app.handlers import cb_ai_actions
    from tests.test_cb_ai_actions import _make_update

    update, query = _make_update(f"{callback}:confirm")
    query.message.text = "Synthetic partial response long enough to continue"
    query.message.reply_text = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
    query.message.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=42), text="Synthetic request")
    tasks = set()
    monkeypatch.setattr(cb_ai_actions, "_background_tasks", tasks)
    monkeypatch.setattr(cb_ai_actions, "_HEAVY_CALLBACK_SEMAPHORE", asyncio.Semaphore(1))
    monkeypatch.setattr(cb_ai_actions, "get_user_chat", AsyncMock(return_value=SimpleNamespace(history=[])))
    monkeypatch.setattr(state, "ensure_state_loaded", AsyncMock())
    monkeypatch.setattr(state, "get_last_sent_message", lambda uid: "Synthetic retry request")
    monkeypatch.setattr(
        "app.voice_intent.detect_tts_intent", AsyncMock(return_value=SimpleNamespace(explicit_tts=False))
    )

    def discard_recovery_metric(coro):
        coro.close()

    monkeypatch.setattr("app.utils.background_tasks.submit_task", discard_recovery_metric)

    async def work(*args, **kwargs):
        await fail_or_complete(outcome)

    monkeypatch.setattr(cb_ai_actions.agent, "_handle_regular_chat", work)
    handler = getattr(cb_ai_actions, f"{callback}_callback")
    await handler(update, SimpleNamespace(user_data={}))
    await asyncio.gather(*tuple(tasks), return_exceptions=True)
    assert_user_counts(collector, 42, int(outcome != "cancelled"), int(outcome == "failed"))


@pytest.mark.asyncio
async def test_voice_pipeline_handled_failure_reaches_its_ingress_owner(monkeypatch):
    from app.handlers import msg_voice
    from app.utils.metrics_middleware import consume_operation_failure, mark_operation_failed

    consume_operation_failure()

    async def pipeline(*args, **kwargs):
        mark_operation_failed()

    monkeypatch.setattr(msg_voice, "_process_voice_pipeline", pipeline)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=SimpleNamespace(voice=object()))
    await msg_voice.handle_voice_inline(SimpleNamespace(), update, SimpleNamespace())
    assert consume_operation_failure() is True
