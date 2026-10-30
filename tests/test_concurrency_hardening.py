import asyncio
import importlib
import logging
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.adapters.concurrency import GlobalLLMSemaphore
from app.errors import UserLimitExceededError


class SemaphoreRedis:
    """Model transport outcomes and membership; real Lua runs in Redis contracts."""

    def __init__(self):
        self.members = set()
        self.renewal_failure = None
        self.renewed = asyncio.Event()

    async def eval(self, script, key_count, key, *args):
        if len(args) == 3:
            _, limit, token = args
            if len(self.members) >= limit:
                return 0
            self.members.add(token)
            return 1
        (token,) = args
        self.renewed.set()
        if self.renewal_failure == "error":
            raise ConnectionError("synthetic renewal outage")
        if self.renewal_failure == "stalled":
            await asyncio.Future()
        return int(token in self.members)

    async def zrem(self, key, token):
        self.members.discard(token)


@pytest.mark.asyncio
async def test_lost_lease_cancels_and_joins_old_provider_before_scope_exit(monkeypatch, caplog):
    """A deleted token cannot leave the old scope running as a local owner."""
    redis = SemaphoreRedis()
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1)
    semaphore._renew_interval = 0.01
    entered, stopping, stopped, finish_stop = (asyncio.Event() for _ in range(4))
    holder_token = []
    renewals = []

    async def provider():
        try:
            await asyncio.Future()
        finally:
            stopping.set()
            await finish_stop.wait()
            stopped.set()

    async def holder():
        async with semaphore:
            holder_token.append(semaphore._token.get())
            renewals.append(semaphore._renewal.get())
            entered.set()
            await provider()
        pytest.fail("a lost owner cannot report successful scope completion")

    caplog.set_level(logging.DEBUG, logger="app.observability")
    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        redis.members.remove(holder_token[0])
        try:
            async with asyncio.timeout(0.2):
                await stopping.wait()
        except TimeoutError:
            pytest.fail("the missing renewal member must cancel its provider owner")
        assert not task.done() and not stopped.is_set()
        assert semaphore._local_semaphore._value == 0

        contender = GlobalLLMSemaphore(1)
        async with contender:
            new_token = contender._token.get()
            finish_stop.set()
            with pytest.raises(UserLimitExceededError) as failure:
                await task
            assert failure.value.details["reason_code"] == "lease_lost"
            assert stopped.is_set() and renewals[0].done()
            assert redis.members == {new_token}

        assert semaphore._local_semaphore._value == 1
        releases = [r for r in caplog.records if getattr(r, "_event_name", "") == "concurrency.released"]
        assert any(getattr(record, "reason_code", None) == "lease_lost" for record in releases)
    finally:
        finish_stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "stalled"])
async def test_unconfirmed_lease_stops_work_by_last_safe_confirmation(monkeypatch, failure):
    """Renewal exceptions and stalls cannot retain ownership beyond its lease."""
    redis = SemaphoreRedis()
    redis.renewal_failure = failure
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1, timeout=0.06)
    semaphore._renew_interval = 0.01
    stopped = asyncio.Event()

    async def holder():
        async with semaphore:
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(redis.renewed.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done, "work cannot run indefinitely without a confirmed lease"
        with pytest.raises(UserLimitExceededError) as error:
            await task
        assert error.value.details["reason_code"] == "lease_lost"
        assert stopped.is_set()
        assert semaphore._local_semaphore._value == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_release_deadline_stops_cleanup_and_preserves_first_cancellation(monkeypatch):
    """Repeated caller cancellation cannot make a stalled release live forever."""
    release_started, release_stopped, body_started = (asyncio.Event() for _ in range(3))
    finish_release = asyncio.Event()
    cleanup_tasks = []
    redis = SemaphoreRedis()

    async def blocked_release(key, token):
        cleanup_tasks.append(asyncio.current_task())
        release_started.set()
        try:
            await finish_release.wait()
        finally:
            release_stopped.set()

    redis.zrem = blocked_release
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1)
    semaphore._cleanup_timeout = 0.03

    async def holder():
        async with semaphore:
            body_started.set()
            await asyncio.Future()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(body_started.wait(), 1)
        task.cancel("original cancellation")
        await asyncio.wait_for(release_started.wait(), 1)
        task.cancel("later cancellation")
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done, "release must end at its independent cleanup deadline"
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert error.value.args == ("original cancellation",)
        assert release_stopped.is_set() and all(child.done() for child in cleanup_tasks)
        assert semaphore._local_semaphore._value == 1
    finally:
        finish_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_external_cancel_during_lease_loss_cleanup_keeps_external_reason(monkeypatch):
    """A lease cancellation marker cannot replace a subsequent shutdown reason."""
    redis = SemaphoreRedis()
    monkeypatch.setattr("app.cache.redis_client", redis)
    entered, stopping, finish_stop, releasing, finish_release = (asyncio.Event() for _ in range(5))
    semaphore = GlobalLLMSemaphore(1)
    semaphore._renew_interval = 0.01
    token = []

    async def blocked_release(key, owned_token):
        releasing.set()
        await finish_release.wait()
        redis.members.discard(owned_token)

    redis.zrem = blocked_release

    async def holder():
        async with semaphore:
            token.append(semaphore._token.get())
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopping.set()
                await finish_stop.wait()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        redis.members.remove(token[0])
        await asyncio.wait_for(stopping.wait(), 1)
        finish_stop.set()
        await asyncio.wait_for(releasing.wait(), 1)
        task.cancel("shutdown request")
        finish_release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert error.value.args == ("shutdown request",)
        assert semaphore._local_semaphore._value == 1
    finally:
        finish_stop.set()
        finish_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_confirmation_on_delayed_admission_never_starts_provider(monkeypatch):
    """A delayed server acknowledgement cannot create a fresh local lease."""
    redis = SemaphoreRedis()
    original_eval = redis.eval

    async def delayed_admission(*args):
        result = await original_eval(*args)
        await asyncio.sleep(0.04)
        return result

    redis.eval = delayed_admission
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1, timeout=1)
    semaphore._lease_ttl = 0.02
    provider_started = False

    with pytest.raises(UserLimitExceededError):
        async with semaphore:
            provider_started = True
            await asyncio.Future()
    assert not provider_started, "an expired local lease confirmation cannot start work"
    assert semaphore._local_semaphore._value == 1
    assert not redis.members


@pytest.mark.asyncio
async def test_lease_loss_racing_body_failure_preserves_provider_exception(monkeypatch):
    """A renewal cancellation delivered in __aexit__ cannot hide a body error."""
    redis = SemaphoreRedis()
    original_eval = redis.eval
    renewal_started, finish_renewal, fail_body = (asyncio.Event() for _ in range(3))

    async def blocked_renewal(script, key_count, key, *args):
        if len(args) == 1:
            renewal_started.set()
            await finish_renewal.wait()
            return 0
        return await original_eval(script, key_count, key, *args)

    redis.eval = blocked_renewal
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1)
    semaphore._renew_interval = 0.01

    async def holder():
        async with semaphore:
            await fail_body.wait()
            raise ValueError("synthetic provider failure")

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(renewal_started.wait(), 1)
        # Schedule the body first, then renewal, so cancellation lands in exit.
        fail_body.set()
        finish_renewal.set()
        with pytest.raises(ValueError, match="synthetic provider failure"):
            await task
        assert semaphore._local_semaphore._value == 1
    finally:
        finish_renewal.set()
        fail_body.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [None, ConnectionError, TimeoutError])
async def test_initial_local_fallback_reports_degraded_scope(monkeypatch, caplog, backend):
    """Only initial Redis absence/failure may admit a labelled degraded scope."""
    redis = None
    if backend is not None:
        redis = AsyncMock()
        redis.eval.side_effect = backend("synthetic initial failure")
    monkeypatch.setattr("app.cache.redis_client", redis)
    caplog.set_level(logging.DEBUG, logger="app.observability")
    semaphore = GlobalLLMSemaphore(1)

    async with semaphore:
        assert semaphore._local_semaphore._value == 0
        assert semaphore._renewal.get() is None

    records = [
        record
        for record in caplog.records
        if getattr(record, "_event_name", "") in {"concurrency.acquire_finished", "concurrency.released"}
    ]
    assert len(records) == 2
    assert all(record.mode == "local_fallback" and record.degraded is True for record in records)
    assert semaphore._local_semaphore._value == 1


@pytest.mark.asyncio
async def test_transient_renewal_errors_recover_inside_confirmed_lease(monkeypatch):
    """A brief outage may recover without falsely revoking a live lease."""
    redis = SemaphoreRedis()
    original_eval = redis.eval
    confirmations = 0
    recovered = asyncio.Event()

    async def flaky_renewal(script, key_count, key, *args):
        nonlocal confirmations
        if len(args) == 1:
            confirmations += 1
            if confirmations < 3:
                raise ConnectionError("synthetic transient renewal failure")
            if confirmations >= 10:
                recovered.set()
        return await original_eval(script, key_count, key, *args)

    redis.eval = flaky_renewal
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1, timeout=1)
    semaphore._lease_ttl = 0.08
    semaphore._renew_interval = 0.01

    async with semaphore:
        # Ten intervals exceed the original lease and require fresh confirmations.
        await asyncio.wait_for(recovered.wait(), 1)
        assert asyncio.current_task().cancelling() == 0
    assert semaphore._local_semaphore._value == 1
    assert not redis.members


@pytest.mark.asyncio
async def test_swallowed_lease_cancellation_cannot_report_scope_success(monkeypatch):
    """A provider swallowing CancelledError still cannot succeed without a lease."""
    redis = SemaphoreRedis()
    monkeypatch.setattr("app.cache.redis_client", redis)
    semaphore = GlobalLLMSemaphore(1)
    semaphore._renew_interval = 0.01
    entered = asyncio.Event()
    token = []

    async def holder():
        async with semaphore:
            token.append(semaphore._token.get())
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        redis.members.remove(token[0])
        with pytest.raises(UserLimitExceededError) as error:
            await asyncio.wait_for(task, 1)
        assert error.value.details["reason_code"] == "lease_lost"
        assert task.cancelling() == 0
        assert semaphore._local_semaphore._value == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_role_custom_retry_registered_once():
    text = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert text.count('pattern="^role_custom_retry$"') == 1


def test_heavy_callback_semaphore_present_and_used():
    # Semaphore is defined in callbacks.py (shared helpers hub)
    hub_source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert "_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore" in hub_source

    # Semaphore is used in cb_ai_actions.py (heavy callbacks)
    actions_source = Path("app/handlers/cb_ai_actions.py").read_text(encoding="utf-8")
    async_with_count = actions_source.count("async with _HEAVY_CALLBACK_SEMAPHORE")
    assert async_with_count >= 2


def test_heavy_message_semaphore_present_and_used():
    # Multi-tier semaphore: both tiers exported from concurrency.py
    concurrency_source = Path("app/adapters/concurrency.py").read_text(encoding="utf-8")
    assert "heavy_request_semaphore = _LazyGlobalLLMSemaphore" in concurrency_source
    assert "ultra_heavy_semaphore = _LazyGlobalLLMSemaphore" in concurrency_source

    # Semaphores are acquired inside agent.py's process_long_request
    agent_source = Path("app/handlers/agent.py").read_text(encoding="utf-8")
    assert "ultra_heavy_semaphore" in agent_source
    assert "heavy_request_semaphore" in agent_source

    # media-group heavy path still has its own semaphore
    media_source = Path("app/handlers/msg_media.py").read_text(encoding="utf-8")
    assert "async with _HEAVY_REQUEST_SEMAPHORE" in media_source


# ── Streaming lock guard tests ───────────────────────────────────────────────


def test_is_user_busy_helper_exists():
    """Verify the _is_user_busy helper is defined in callbacks.py."""
    source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert "def _is_user_busy(user_id" in source
    assert "_BUSY_TOAST" in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name,handler_name,data,initial_answer",
    [
        ("cb_models", "model_button_callback", "model:0", call()),
        ("cb_models", "switch_model_callback", "switch_model:gemini-3.1-flash-lite", call()),
        ("cb_navigation", "new_topic_callback", "new_topic", call("...")),
        ("cb_navigation", "new_chat_callback", "new_chat", None),
        ("cb_navigation", "deep_dive_callback", "deep_dive:new_topic", call()),
        ("cb_navigation", "toggle_search_callback", "toggle_search", None),
    ],
    ids=["model", "switch_model", "new_topic", "new_chat", "deep_dive", "toggle_search"],
)
async def test_busy_state_mutating_callbacks_have_no_effects(module_name, handler_name, data, initial_answer):
    from app.handlers.callbacks import _BUSY_TOAST

    module = importlib.import_module(f"app.handlers.{module_name}")
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.from_user.id = 73421
    update.effective_user.language_code = "ru"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"model_list": ["gemini-3.1-flash-lite"], "sentinel": "keep"}
    before = deepcopy(context.user_data)
    lock = asyncio.Lock()
    await lock.acquire()
    with ExitStack() as stack:
        lock_reader = stack.enter_context(patch("app.handlers.callbacks.state.get_user_lock", return_value=lock))
        get_chat = stack.enter_context(patch.object(module, "get_user_chat", new_callable=AsyncMock))
        update_chat = stack.enter_context(patch.object(module, "update_user_chat", new_callable=AsyncMock))
        menus = stack.enter_context(patch.object(module, "menus"))
        submit = stack.enter_context(patch("app.utils.background_tasks.submit_task"))
        try:
            await getattr(module, handler_name)(update, context)
        finally:
            lock.release()

    lock_reader.assert_called_once_with(73421)
    expected_answers = [] if initial_answer is None else [initial_answer]
    assert query.answer.await_args_list == expected_answers + [call(_BUSY_TOAST, show_alert=True)]
    get_chat.assert_not_called()
    update_chat.assert_not_called()
    assert menus.mock_calls == []
    submit.assert_not_called()
    query.edit_message_text.assert_not_called()
    query.edit_message_reply_markup.assert_not_called()
    query.message.reply_text.assert_not_called()
    assert context.user_data == before
