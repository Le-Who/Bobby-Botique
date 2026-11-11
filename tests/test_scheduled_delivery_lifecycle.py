"""Scheduled work must retain its owner until a confirmed terminal delivery."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message
from telegram.error import NetworkError

from app.errors import ErrorCode
from app.response_delivery.outcomes import CompleteDelivery, FailedDelivery, PartialDelivery
from app.response_delivery.renderer import DeliveryKind, DeliveryReceipt, TelegramMessageRef
from tests.scheduled_delivery_fakes import allowed_private_data, install_scheduled_journal


@pytest.fixture
def scheduled_boundary(monkeypatch):
    state = install_scheduled_journal(monkeypatch)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", allowed_private_data)
    return state


def message():
    return Message(message_id=71, date=datetime.now(UTC), chat=Chat(id=42, type="private"))


def complete():
    return CompleteDelivery(
        content_text="Answer",
        displayed_text="Answer",
        completion=None,
        voice_requested=False,
        receipt=DeliveryReceipt(DeliveryKind.MESSAGE, (71,), TelegramMessageRef(42, 71)),
    )


@pytest.mark.asyncio
async def test_ai_admission_is_not_delivery(monkeypatch):
    from app.handlers import cmd_reminders as reminders

    entered = asyncio.Event()
    tasks = []

    async def held_worker(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    def submit(coroutine, **kwargs):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    row = {"id": 7, "user_id": 42, "prompt": "Find news", "context_history": {"is_ai": True}}
    monkeypatch.setattr(reminders, "get_pending_reminders", AsyncMock(return_value=[row]))
    monkeypatch.setattr(reminders, "_execute_ai_reminder", held_worker)
    monkeypatch.setattr(reminders, "try_submit_task", submit)
    mark = AsyncMock()
    monkeypatch.setattr(reminders.scheduled, "finish_delivery", mark)
    # The durable claim boundary is synthetic here; actual SQL concurrency is
    # covered by the disposable PostgreSQL contracts.
    ledger = SimpleNamespace(
        claim_reminder=AsyncMock(return_value={"delivery_id": "claim"}),
        recover_abandoned=AsyncMock(),
    )
    monkeypatch.setattr(reminders, "scheduled", ledger, raising=False)
    try:
        await reminders.check_and_deliver_reminders(SimpleNamespace(bot=AsyncMock()))
        await entered.wait()
        mark.assert_not_awaited()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_two_reminder_pollers_respect_one_durable_claim(monkeypatch):
    from app.handlers import cmd_reminders as reminders

    row = {"id": 7, "user_id": 42, "prompt": "A reminder", "context_history": None}
    monkeypatch.setattr(reminders, "get_pending_reminders", AsyncMock(return_value=[row]))
    ledger = SimpleNamespace(
        claim_reminder=AsyncMock(side_effect=[{"delivery_id": "claim"}, None]),
        recover_abandoned=AsyncMock(),
    )
    monkeypatch.setattr(reminders, "scheduled", ledger, raising=False)
    delivery = AsyncMock()
    monkeypatch.setattr(reminders, "run_scheduled_delivery", delivery, raising=False)
    bot = AsyncMock()
    context = SimpleNamespace(bot=bot)
    await asyncio.gather(reminders.check_and_deliver_reminders(context), reminders.check_and_deliver_reminders(context))
    assert bot.send_message.await_count + delivery.await_count == 1


@pytest.mark.asyncio
async def test_brief_subscription_is_captured_before_private_generation(monkeypatch):
    from app.handlers import scheduled_briefs as briefs

    captured = []

    async def no_claim(*args, **kwargs):
        captured.append("subscription_checked")
        return None

    topics = AsyncMock(return_value=(["topic"], 9))
    monkeypatch.setattr(briefs, "_get_user_topics_snapshot", topics)
    monkeypatch.setattr(briefs, "_search_for_topics", AsyncMock(return_value=[]))
    monkeypatch.setattr(briefs, "_generate_brief_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(briefs, "scheduled", SimpleNamespace(claim_brief=no_claim), raising=False)
    assert await briefs.generate_and_send_brief(42, AsyncMock()) is False
    assert captured == ["subscription_checked"]
    topics.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_placeholder_alone_is_quarantined(scheduled_boundary):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))

    async def generate(owner):
        await owner.send_message(chat_id=42, text="Working")
        raise RuntimeError("synthetic provider interruption")

    assert not await run_scheduled_delivery(claim, bot, generate, ai_response=True)
    assert scheduled_boundary.finished[-1][1].outcome == "unknown_delivery"
    assert await journal.claim_reminder(7) is None


@pytest.mark.asyncio
async def test_ai_final_edit_receipt_and_typed_outcome_acknowledge(scheduled_boundary):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=message()),
        edit_message_text=AsyncMock(return_value=message()),
    )

    async def generate(owner):
        placeholder = await owner.send_message(chat_id=42, text="Working")
        assert placeholder.get_bot() is owner
        final = await placeholder.edit_text("Answer")
        assert final.get_bot() is owner
        owner.observe_response(complete())

    assert await run_scheduled_delivery(claim, bot, generate, ai_response=True)
    assert scheduled_boundary.finished[-1][1].outcome == "sent"
    assert len(scheduled_boundary.attempts) == 2
    bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, expected",
    [
        (FailedDelivery(ErrorCode.GENERIC, "Failure", complete().receipt), "failed"),
        (PartialDelivery("partial", "partial", None, False, complete().receipt), "unknown_delivery"),
    ],
)
async def test_ai_terminal_outcome_controls_marker(scheduled_boundary, outcome, expected):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))

    async def generate(owner):
        await owner.send_message(chat_id=42, text="Response")
        owner.observe_response(outcome)

    assert not await run_scheduled_delivery(claim, bot, generate, ai_response=True)
    assert scheduled_boundary.finished[-1][1].outcome == expected


@pytest.mark.asyncio
async def test_ambiguous_final_edit_blocks_swallowed_fallback(scheduled_boundary):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=message()),
        edit_message_text=AsyncMock(side_effect=NetworkError("synthetic transport loss")),
    )

    async def generate(owner):
        placeholder = await owner.send_message(chat_id=42, text="Working")
        try:
            await placeholder.edit_text("Answer")
        except NetworkError:
            await owner.send_message(chat_id=42, text="fallback")

    assert not await run_scheduled_delivery(claim, bot, generate, ai_response=True)
    assert scheduled_boundary.finished[-1][1].outcome == "unknown_delivery"
    bot.send_message.assert_awaited_once()
    assert len(scheduled_boundary.attempts) == 2


@pytest.mark.asyncio
async def test_unknown_attempt_commit_never_runs_fallback(scheduled_boundary, monkeypatch):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    monkeypatch.setattr(journal, "finish_attempt", AsyncMock(return_value=False))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))

    async def generate(owner):
        try:
            await owner.send_message(chat_id=42, text="Answer")
        except Exception:
            await owner.send_message(chat_id=42, text="fallback")

    assert not await run_scheduled_delivery(claim, bot, generate)
    assert scheduled_boundary.finished[-1][1].reason_code == "journal_commit_unconfirmed"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["unsubscribe", "reschedule"])
async def test_brief_generation_cannot_survive_subscription_change(scheduled_boundary, monkeypatch, mutation):
    from app.handlers import scheduled_briefs as briefs

    started = asyncio.Event()
    release = asyncio.Event()

    async def generate(*args, **kwargs):
        started.set()
        await release.wait()
        return {"Topic": "Summary"}

    async def update_subscription(*args, **kwargs):
        scheduled_boundary.current = False

    monkeypatch.setattr(briefs, "_get_user_topics_snapshot", AsyncMock(return_value=(["topic"], 9)))
    monkeypatch.setattr(briefs, "_search_for_topics", AsyncMock(return_value=[]))
    monkeypatch.setattr(briefs, "_generate_brief_summary", generate)
    monkeypatch.setattr(briefs, "_is_ltm_snapshot_current", AsyncMock(return_value=True))
    monkeypatch.setattr(briefs.db, "db_query", update_subscription)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))
    task = asyncio.create_task(briefs.generate_and_send_brief(42, bot))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if mutation == "unsubscribe":
            assert await briefs.deactivate_subscription(42)
        else:
            assert await briefs.upsert_subscription(42, preferred_hour=8)
        release.set()
        assert not await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    bot.send_message.assert_not_awaited()
    assert scheduled_boundary.finished[-1][1].outcome == "skipped"


@pytest.mark.asyncio
async def test_two_brief_pollers_generate_once(scheduled_boundary, monkeypatch):
    from app.handlers import scheduled_briefs as briefs

    topics = AsyncMock(return_value=(["topic"], 9))
    monkeypatch.setattr(briefs, "_get_user_topics_snapshot", topics)
    monkeypatch.setattr(briefs, "_search_for_topics", AsyncMock(return_value=[]))
    monkeypatch.setattr(briefs, "_generate_brief_summary", AsyncMock(return_value={"Topic": "Summary"}))
    monkeypatch.setattr(briefs, "_is_ltm_snapshot_current", AsyncMock(return_value=True))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))
    results = await asyncio.gather(briefs.generate_and_send_brief(42, bot), briefs.generate_and_send_brief(42, bot))
    assert sorted(results) == [False, True]
    topics.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_account_cannot_start_reminder_pipeline(scheduled_boundary, monkeypatch):
    from app.handlers import cmd_reminders as reminders
    from app.repos import scheduled_deliveries as journal

    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=SimpleNamespace(memory_epoch=10)))
    search = AsyncMock()
    monkeypatch.setattr("app.handlers.ai_search._handle_qna_search", search)
    claim = await journal.claim_reminder(7)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))
    await reminders._execute_ai_reminder(bot, 42, "Find news", "qna", 7, claim=claim)
    bot.send_message.assert_not_awaited()
    search.assert_not_awaited()
    assert scheduled_boundary.finished[-1][1].outcome != "sent"


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_disposition_commit(scheduled_boundary, monkeypatch):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    started = asyncio.Event()
    release = asyncio.Event()
    committed = asyncio.Event()

    async def settle(*args):
        started.set()
        await release.wait()
        committed.set()
        return True

    monkeypatch.setattr(journal, "finish_delivery", settle)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))

    async def generate(owner):
        await owner.send_message(chat_id=42, text="Reminder")

    task = asyncio.create_task(run_scheduled_delivery(claim, bot, generate))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert committed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("sent_placeholder", [False, True])
async def test_repeated_cancel_drains_helper_before_releasing_or_settling(
    scheduled_boundary, monkeypatch, sent_placeholder
):
    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    claim = await journal.claim_reminder(7)
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))

    async def helper(owner):
        if sent_placeholder:
            await owner.send_message(chat_id=42, text="Working")
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    task = asyncio.create_task(run_scheduled_delivery(claim, bot, helper, ai_response=True))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        for _ in range(8):
            await asyncio.sleep(0)
        premature = task.done()
        released_early = journal.release_unstarted.await_count > 0
        settled_early = bool(scheduled_boundary.finished)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not premature
    assert not released_early
    assert not settled_early
    assert cleaned.is_set()
    assert task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [True, False])
async def test_unavailable_lease_is_retryable_only_for_a_current_claim(scheduled_boundary, monkeypatch, current):
    from contextlib import asynccontextmanager

    from app.repos import scheduled_deliveries as journal
    from app.scheduled_delivery import run_scheduled_delivery

    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        yield False

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", unavailable)
    claim = await journal.claim_reminder(7)
    scheduled_boundary.current = current
    helper = AsyncMock()
    assert not await run_scheduled_delivery(claim, SimpleNamespace(), helper)
    helper.assert_not_awaited()
    result = scheduled_boundary.finished[-1][1]
    assert result.outcome == ("failed" if current else "skipped")
    assert result.retryable is current


@pytest.mark.asyncio
async def test_hourly_brief_poll_retries_original_slot_after_preferred_hour(scheduled_boundary, monkeypatch):
    from app.handlers import scheduled_briefs as briefs
    from app.repos import scheduled_deliveries as journal

    class EightOClock:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 11, 11, 8, 1, tzinfo=UTC)

    monkeypatch.setattr(briefs, "datetime", EightOClock)
    claim = await journal.claim_brief(42)
    claim.update(source_id=19, source_revision=4, slot_date=datetime(2026, 11, 11, tzinfo=UTC).date())
    retry = dict(claim, state="failed", retryable=True, preferred_hour=7)
    poll_retries = AsyncMock(return_value=[retry])
    reclaim = AsyncMock(return_value=claim)
    monkeypatch.setattr(journal, "get_due_brief_retries", poll_retries, raising=False)
    monkeypatch.setattr(journal, "claim_brief_retry", reclaim, raising=False)
    journal.claim_brief.reset_mock()
    monkeypatch.setattr(briefs, "get_due_subscriptions", AsyncMock(return_value=[]))
    monkeypatch.setattr(briefs, "_get_user_topics_snapshot", AsyncMock(return_value=(["topic"], 9)))
    monkeypatch.setattr(briefs, "_search_for_topics", AsyncMock(return_value=[]))
    monkeypatch.setattr(briefs, "_generate_brief_summary", AsyncMock(return_value={"Topic": "Summary"}))
    monkeypatch.setattr(briefs, "_is_ltm_snapshot_current", AsyncMock(return_value=True))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message()))

    await briefs.check_and_send_briefs(SimpleNamespace(bot=bot))

    poll_retries.assert_awaited_once()
    reclaim.assert_awaited_once_with(retry)
    journal.claim_brief.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert scheduled_boundary.finished[-1][0]["slot_date"] == retry["slot_date"]
    assert scheduled_boundary.finished[-1][0]["source_revision"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("require_ltm", [False, True])
async def test_research_voice_keeps_its_owner_after_scheduled_text_settles(
    scheduled_boundary,
    monkeypatch,
    require_ltm,
):
    from app.core.agentic import AgenticResult
    from app.handlers import ai_search
    from app.handlers import cmd_reminders as reminders
    from app.repos import scheduled_deliveries as journal
    from app.voice_intent import build_voice_source_key

    release_voice = asyncio.Event()
    voice_tasks = []
    captured = []
    state = SimpleNamespace(
        memory_epoch=9,
        _has_persisted_chat=True,
        ltm_enabled=require_ltm,
        model="gemini-3.1-flash-lite",
        history=[],
        thinking_level=None,
        voice_id="synthetic-voice",
        tts_temperature=0.5,
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=message()),
        edit_message_text=AsyncMock(return_value=message()),
        send_voice=AsyncMock(return_value=message()),
    )

    async def completed_delivery(target, response, **kwargs):
        await target.placeholder_message.edit_text(response.content_text)
        return complete()

    async def enqueue_voice(**kwargs):
        captured.append(kwargs)

        async def delayed_voice():
            await release_voice.wait()
            await kwargs["bot"].send_voice(chat_id=kwargs["chat_id"], voice=b"synthetic voice")

        voice_tasks.append(asyncio.create_task(delayed_voice()))
        return SimpleNamespace(queued=True)

    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=state))
    monkeypatch.setattr(ai_search, "ensure_chat_generation", AsyncMock(return_value=9))
    monkeypatch.setattr(ai_search, "update_user_chat", AsyncMock())
    monkeypatch.setattr(ai_search, "_retire_placeholder_notice", AsyncMock())
    monkeypatch.setattr(ai_search.metrics_collector, "record_search_query", AsyncMock())
    monkeypatch.setattr(ai_search, "_available_models", list)
    monkeypatch.setattr(
        "app.process_policies.resolve_process",
        AsyncMock(return_value=SimpleNamespace(models=(state.model,), explicit=False)),
    )
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "synthetic-key", "key_hash": "synthetic-hash"}),
    )
    monkeypatch.setattr(
        ai_search,
        "AgenticSearch",
        lambda **kwargs: SimpleNamespace(run=AsyncMock(return_value=AgenticResult(answer="A" * 250))),
    )
    monkeypatch.setattr(
        "app.response_delivery.delivery.get_telegram_response_delivery",
        lambda: SimpleNamespace(deliver=completed_delivery),
    )
    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue_voice)
    claim = await journal.claim_reminder(7)
    try:
        await reminders._execute_ai_reminder(bot, 42, "Research news", "research", 7, claim=claim)
        assert scheduled_boundary.finished[-1][1].outcome == "sent"
        assert len(captured) == 1
        queued = captured[0]
        assert queued["bot"] is bot
        assert queued["expected_epoch"] == 9
        assert queued["require_ltm"] is require_ltm
        assert queued["user_id"] == queued["chat_id"] == 42
        assert queued["reply_to_message_id"] == 71
        assert queued["source_key"] == build_voice_source_key("research_tts", 42, 71)
        assert queued["voice"] == "synthetic-voice"
        scheduled_boundary.current = False  # Text claim is now terminal.
        release_voice.set()
        await asyncio.gather(*voice_tasks)
        bot.send_voice.assert_awaited_once()
        assert len(scheduled_boundary.attempts) == 2  # Placeholder and final edit.
    finally:
        release_voice.set()
        await asyncio.gather(*voice_tasks, return_exceptions=True)
