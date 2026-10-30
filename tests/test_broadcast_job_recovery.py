"""Broadcast effects are journaled before Telegram and uncertain effects never replay."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from app.broadcast_delivery import DeliveryOwner
from app.broadcast_jobs import BroadcastService
from app.queue_contracts import AdmissionState, QueueAdmission


class JournalProbe:
    def __init__(self):
        self.events = []
        self.attempts = []
        self.fail_start = False
        self.fail_finish = False

    async def start_attempt(self, batch_id, recipient_id, token, sequence):
        self.events.append("started")
        if self.fail_start:
            raise ConnectionError("synthetic database disconnect")
        attempt = {"attempt_id": str(sequence)}
        self.attempts.append(attempt)
        return attempt

    async def finish_attempt(self, attempt_id, token, result):
        self.events.append(result.outcome)
        if self.fail_finish:
            raise ConnectionError("synthetic lost commit reply")
        return True


def owner(journal, bot, *, checkpoint=None, sleeper=None):
    return DeliveryOwner(
        journal,
        batch_id="batch",
        recipient_id=101,
        token="owner",
        bot=bot,
        checkpoint=checkpoint or AsyncMock(),
        sleeper=sleeper or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_started_is_durable_before_each_external_send_and_confirmed_id_saved():
    journal = JournalProbe()

    async def send(**kwargs):
        assert journal.events == ["started"]
        return SimpleNamespace(message_id=42)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    result = await owner(journal, bot).deliver(lambda proxy: proxy.send_message(chat_id=101, text="synthetic"))
    assert (result.outcome, result.message_id) == ("sent", 42)
    assert journal.events == ["started", "sent"]


@pytest.mark.asyncio
async def test_timeout_after_possible_send_blocks_existing_photo_text_fallback():
    journal = JournalProbe()
    bot = SimpleNamespace(send_photo=AsyncMock(side_effect=TimedOut()), send_message=AsyncMock())

    async def existing_helper(proxy):
        try:
            await proxy.send_photo(chat_id=101, photo="synthetic")
        except Exception:
            await proxy.send_message(chat_id=101, text="fallback")

    result = await owner(journal, bot).deliver(existing_helper)
    assert result.outcome == "unknown_delivery"
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert len(journal.attempts) == 1


@pytest.mark.asyncio
async def test_definite_media_rejection_preserves_safe_existing_fallback():
    journal = JournalProbe()
    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=BadRequest("wrong file identifier")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=43)),
    )

    async def helper(proxy):
        try:
            await proxy.send_photo(chat_id=101, photo="bad")
        except BadRequest:
            await proxy.send_message(chat_id=101, text="fallback")

    result = await owner(journal, bot).deliver(helper)
    assert result.outcome == "sent"
    assert journal.events == ["started", "failed", "started", "sent"]


@pytest.mark.asyncio
async def test_start_write_failure_never_calls_telegram():
    journal = JournalProbe()
    journal.fail_start = True
    bot = SimpleNamespace(send_message=AsyncMock())
    result = await owner(journal, bot).deliver(lambda proxy: proxy.send_message(text="synthetic"))
    assert result.outcome == "failed"
    assert result.retryable is True
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_lost_after_confirmed_send_quarantines_recipient_without_replay():
    journal = JournalProbe()
    journal.fail_finish = True
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=44)))
    result = await owner(journal, bot).deliver(lambda proxy: proxy.send_message(text="synthetic"))
    assert (result.outcome, result.reason_code) == ("unknown_delivery", "journal_commit_unconfirmed")
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_is_definite_reject_and_each_bounded_retry_is_journaled(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    journal = JournalProbe()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RetryAfter(2), SimpleNamespace(message_id=45)]))
    sleep = AsyncMock()
    result = await owner(journal, bot, sleeper=sleep).deliver(lambda proxy: proxy.send_message(text="synthetic"))
    assert result.outcome == "sent"
    assert len(journal.attempts) == 2
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_blocked_user_is_definite_nonretryable_failure():
    journal = JournalProbe()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("bot was blocked by the user")))
    result = await owner(journal, bot).deliver(lambda proxy: proxy.send_message(text="synthetic"))
    assert (result.outcome, result.reason_code, result.retryable) == ("failed", "recipient_unreachable", False)


@pytest.mark.asyncio
async def test_arbitrary_cancellation_in_telegram_is_unknown_even_if_helper_swallows_errors():
    journal = JournalProbe()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.CancelledError()))
    result = await owner(journal, bot).deliver(lambda proxy: proxy.send_message(text="synthetic"))
    assert result.outcome == "unknown_delivery"
    assert journal.events == ["started", "unknown_delivery"]


class ServiceJournal:
    def __init__(self):
        self.batches = {}
        self.snapshots = {}
        self.rows = {}
        self.sent = []
        self.erased = set()
        self.active = {101: 7, 102: 8}

    async def read_by_operation(self, operation_id):
        return next((b.copy() for b in self.batches.values() if b["operation_id"] == operation_id), None)

    async def candidates(self, channel, user_ids, *, mode, force=False, variant=""):
        return [
            {
                "recipient_id": u,
                "privacy_epoch": self.active.get(u),
                "reason_code": None if u in self.active else "account_missing",
            }
            for u in user_ids
        ]

    async def create_batch(self, **kwargs):
        row = {
            **kwargs,
            "task_id": None,
            "state": "draft",
            "created_at": None,
            "total": len(kwargs["recipients"]),
            "pending": len(kwargs["recipients"]),
            "started": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "unknown_delivery": 0,
            "retryable_failed": 0,
        }
        self.batches[row["batch_id"]] = row
        self.snapshots[row["batch_id"]] = kwargs["recipients"]
        return {**row, "_created": True}

    async def bind_task(self, batch_id, task_id):
        self.batches[batch_id]["task_id"] = task_id
        return True

    async def record_admission(self, batch_id, admission):
        self.batches[batch_id].update(
            task_id=admission.task_id,
            admission_state=str(admission.state),
            admission_reason=admission.reason_code,
            storage_mode=admission.storage_mode,
        )
        if admission.state is AdmissionState.REJECTED:
            row = self.batches[batch_id]
            row.update(state="finished", failed=row["pending"], retryable_failed=row["pending"], pending=0)

    async def read_batch(self, batch_id):
        return self.batches.get(batch_id)

    async def coverage_start(self):
        return None

    async def recipients(self, batch_id, *, retryable_only=False):
        if retryable_only:
            return [{"recipient_id": 101, "privacy_epoch": 7}]
        return self.snapshots[batch_id]


@pytest.mark.asyncio
async def test_enqueue_snapshot_is_idempotent_even_when_repeated_body_changes():
    journal = ServiceJournal()
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))
    service = BroadcastService(journal=journal, queue=queue)
    first = await service.enqueue_offers(
        channel="tarot", user_ids=[101], operation_id="00000000-0000-4000-8000-000000000001"
    )
    second = await service.enqueue_offers(
        channel="horoscope", user_ids=[102], operation_id="00000000-0000-4000-8000-000000000001"
    )
    assert first["batch"]["batch_id"] == second["batch"]["batch_id"]
    assert second["batch"]["channel"] == "tarot"
    assert len(journal.batches) == 1
    assert queue.enqueue_job.await_count == 1
    assert list(journal.snapshots.values())[0][0]["recipient_id"] == 101


@pytest.mark.asyncio
async def test_queue_unknown_returns_persistent_batch_identity_and_no_direct_send():
    journal = ServiceJournal()
    queue = SimpleNamespace(
        enqueue_job=AsyncMock(
            return_value=QueueAdmission(AdmissionState.UNKNOWN, "stable-task", "admission_unconfirmed", "redis")
        )
    )
    service = BroadcastService(journal=journal, queue=queue)
    result = await service.enqueue_offers(
        channel="tarot", user_ids=[101], operation_id="00000000-0000-4000-8000-000000000002"
    )
    assert result["admission"]["state"] == "unknown"
    assert result["admission"]["batch_id"] == result["batch"]["batch_id"]
    assert result["batch"]["task_id"] == "stable-task"
    assert result["batch"]["mode"] == "offer"


@pytest.mark.asyncio
async def test_retry_new_batch_only_safe_failed_recipients_rechecks_original_epoch():
    journal = ServiceJournal()
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))
    service = BroadcastService(journal=journal, queue=queue)
    first = await service.enqueue_offers(
        channel="tarot", user_ids=[101, 102], operation_id="00000000-0000-4000-8000-000000000003"
    )
    parent = first["batch"]["batch_id"]
    journal.batches[parent]["state"] = "finished"
    journal.active[101] = 70  # erased/recreated: never recapture old snapshot into this new epoch
    retry = await service.retry_failed(parent, operation_id="00000000-0000-4000-8000-000000000004")
    child = retry["batch"]
    assert child["parent_batch_id"] == parent
    child_rows = journal.snapshots[child["batch_id"]]
    assert len(child_rows) == 1
    assert child_rows[0]["reason_code"] == "privacy_revoked"


@pytest.mark.asyncio
async def test_offer_counts_never_feed_observed_actual_delivery_counts():
    journal = ServiceJournal()
    journal.observed_counts = AsyncMock(return_value={"sent": 0, "batches": 0})
    service = BroadcastService(journal=journal, queue=object())
    result = await service.observed_channel_counts("tarot")
    assert result["sent"] == 0
    journal.observed_counts.assert_awaited_once_with("tarot", day=None, mode="delivery")


@pytest.mark.asyncio
async def test_definite_admission_rejection_is_terminal_and_same_operation_never_resurrects():
    journal = ServiceJournal()
    queue = SimpleNamespace(
        enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.REJECTED, None, "job_capacity"))
    )
    service = BroadcastService(journal=journal, queue=queue)
    args = {"channel": "tarot", "user_ids": [101], "operation_id": "00000000-0000-4000-8000-000000000005"}
    first = await service.enqueue_offers(**args)
    queue.enqueue_job.return_value = QueueAdmission(AdmissionState.ACCEPTED, "new-task")
    second = await service.enqueue_offers(**args)
    assert first["admission"]["state"] == second["admission"]["state"] == "rejected"
    assert second["batch"]["state"] == "finished"
    assert second["batch"]["retryable_failed"] == 1
    assert queue.enqueue_job.await_count == 1


class ExecutionJournal(ServiceJournal):
    def __init__(self):
        super().__init__()
        self.active[103] = 9
        self.recipient_states = {}
        self.attempt_results = {}
        self.attempt_tokens = {}
        self.candidate_reason = None

    async def candidates(self, *args, **kwargs):
        rows = await super().candidates(*args, **kwargs)
        for row in rows:
            row["reason_code"] = self.candidate_reason
        return rows

    async def claim_batch(self, batch_id, task_id, token, **kwargs):
        row = self.batches[batch_id]
        if row["state"] not in {"draft", "queued"}:
            return None
        row.update(state="running", owner_token=token, force_offer=row["force"])
        return row.copy()

    async def claim_recipient(self, batch_id, recipient_id, batch_token, token):
        if (batch_id, recipient_id) in self.recipient_states:
            return False
        self.recipient_states[batch_id, recipient_id] = token
        self.batches[batch_id]["pending"] -= 1
        self.batches[batch_id]["started"] += 1
        return True

    async def start_attempt(self, batch_id, recipient_id, token, sequence):
        attempt_id = f"{recipient_id}:{sequence}"
        self.attempt_tokens[attempt_id] = token
        return {"attempt_id": attempt_id}

    async def finish_attempt(self, attempt_id, token, result):
        if self.attempt_tokens.get(attempt_id) != token:
            return False
        self.attempt_results[attempt_id] = result
        return True

    async def finish_recipient(self, batch_id, recipient_id, token, result):
        if self.recipient_states.get((batch_id, recipient_id)) != token:
            return False
        self.recipient_states[batch_id, recipient_id] = result.outcome
        row = self.batches[batch_id]
        row["started"] -= 1
        row[result.outcome] += 1
        return True

    async def finish_batch(self, batch_id, token):
        self.batches[batch_id]["state"] = "finished"
        return True

    async def interrupt_batch(self, batch_id):
        row = self.batches[batch_id]
        row.update(
            state="finished",
            unknown_delivery=row["unknown_delivery"] + row["started"],
            skipped=row["skipped"] + row["pending"],
            started=0,
            pending=0,
        )

    async def open_batches(self, limit=100):
        return [row for row in self.batches.values() if row["state"] != "finished"][:limit]

    async def purge_retained_recipients(self):
        return None


@asynccontextmanager
async def allowed_lease(*args, **kwargs):
    yield True


@pytest.mark.asyncio
async def test_real_batch_owner_partial_delivery_and_late_execution_make_exactly_three_calls(monkeypatch):
    journal = ExecutionJournal()
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))

    async def send(**kwargs):
        if kwargs["chat_id"] == 102:
            raise Forbidden("bot was blocked")
        if kwargs["chat_id"] == 103:
            raise TimedOut()
        return SimpleNamespace(message_id=501)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    service = BroadcastService(journal=journal, queue=queue, bot_provider=lambda: bot)
    batch = await service.enqueue_offers(
        channel="tarot", user_ids=[101, 102, 103], operation_id="00000000-0000-4000-8000-000000000006"
    )
    guard = SimpleNamespace(
        snapshot=SimpleNamespace(task_id="task"), checkpoint=AsyncMock(), update_progress=AsyncMock()
    )
    monkeypatch.setattr("app.queue.current_job_guard", lambda: guard)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", allowed_lease)
    monkeypatch.setattr("app.repos.tarot_daily_subscriptions.mark_tarot_discovery_sent", AsyncMock())
    result = await service.execute(batch["batch"]["batch_id"])
    assert result.outcome.value == "unknown_delivery"
    assert journal.batches[batch["batch"]["batch_id"]]["sent"] == 1
    assert journal.batches[batch["batch"]["batch_id"]]["failed"] == 1
    assert journal.batches[batch["batch"]["batch_id"]]["unknown_delivery"] == 1
    assert bot.send_message.await_count == 3
    assert result.result_summary["completed"] == 3
    await service.execute(batch["batch"]["batch_id"])
    assert bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_unsubscribe_between_snapshot_and_send_skips_without_telegram(monkeypatch):
    journal = ExecutionJournal()
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))
    bot = SimpleNamespace(send_message=AsyncMock())
    service = BroadcastService(journal=journal, queue=queue, bot_provider=lambda: bot)
    batch = await service.enqueue_offers(
        channel="tarot", user_ids=[101], operation_id="00000000-0000-4000-8000-000000000007"
    )
    journal.candidate_reason = "unsubscribed"
    guard = SimpleNamespace(
        snapshot=SimpleNamespace(task_id="task"), checkpoint=AsyncMock(), update_progress=AsyncMock()
    )
    monkeypatch.setattr("app.queue.current_job_guard", lambda: guard)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", allowed_lease)
    await service.execute(batch["batch"]["batch_id"])
    bot.send_message.assert_not_awaited()
    assert journal.batches[batch["batch"]["batch_id"]]["skipped"] == 1


@pytest.mark.asyncio
async def test_unconfirmed_admission_recovery_never_enqueues_or_sends_again():
    journal = ExecutionJournal()
    queue = SimpleNamespace(
        enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.UNKNOWN, "task", "admission_unconfirmed")),
        read_job=AsyncMock(return_value=None),
        read_job_by_operation=AsyncMock(return_value=None),
    )
    service = BroadcastService(journal=journal, queue=queue)
    result = await service.enqueue_offers(
        channel="tarot", user_ids=[101], operation_id="00000000-0000-4000-8000-000000000008"
    )
    batch_id = result["batch"]["batch_id"]
    journal.batches[batch_id]["created_at"] = datetime.now(UTC) - timedelta(minutes=2)
    assert await service.recover() == {"recovered": 1}
    assert journal.batches[batch_id]["state"] == "finished"
    assert journal.batches[batch_id]["skipped"] == 1
    assert queue.enqueue_job.await_count == 1


@pytest.mark.asyncio
async def test_erasure_recreate_before_worker_lease_rejects_old_epoch(monkeypatch):
    journal = ExecutionJournal()
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))
    bot = SimpleNamespace(send_message=AsyncMock())
    service = BroadcastService(journal=journal, queue=queue, bot_provider=lambda: bot)
    result = await service.enqueue_offers(
        channel="tarot", user_ids=[101], operation_id="00000000-0000-4000-8000-000000000009"
    )
    journal.active[101] = 700
    epochs = []

    @asynccontextmanager
    async def lease(user_id, epoch, **kwargs):
        epochs.append(epoch)
        yield epoch == journal.active[user_id]

    guard = SimpleNamespace(
        snapshot=SimpleNamespace(task_id="task"), checkpoint=AsyncMock(), update_progress=AsyncMock()
    )
    monkeypatch.setattr("app.queue.current_job_guard", lambda: guard)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    await service.execute(result["batch"]["batch_id"])
    assert epochs == [7]
    assert journal.batches[result["batch"]["batch_id"]]["skipped"] == 1
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipient_parallelism_is_five_and_markers_finish_before_lease_release(monkeypatch):
    journal = ExecutionJournal()
    journal.active = dict.fromkeys(range(101, 111), 7)
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))
    active = 0
    maximum = 0
    leased = set()

    async def send(**kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(message_id=kwargs["chat_id"])

    @asynccontextmanager
    async def lease(user_id, epoch, **kwargs):
        leased.add(user_id)
        try:
            yield True
        finally:
            leased.remove(user_id)

    async def acknowledge(user_id):
        assert user_id in leased

    service = BroadcastService(
        journal=journal, queue=queue, bot_provider=lambda: SimpleNamespace(send_message=AsyncMock(side_effect=send))
    )
    result = await service.enqueue_offers(
        channel="tarot", user_ids=list(journal.active), operation_id="00000000-0000-4000-8000-000000000010"
    )
    guard = SimpleNamespace(
        snapshot=SimpleNamespace(task_id="task"), checkpoint=AsyncMock(), update_progress=AsyncMock()
    )
    monkeypatch.setattr("app.queue.current_job_guard", lambda: guard)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr("app.repos.tarot_daily_subscriptions.mark_tarot_discovery_sent", acknowledge)
    await service.execute(result["batch"]["batch_id"])
    assert maximum == 5 and not leased
    assert journal.batches[result["batch"]["batch_id"]]["sent"] == 10


@pytest.mark.asyncio
async def test_two_first_calls_have_only_one_admission_owner_even_after_rejection():
    class RaceJournal(ServiceJournal):
        def __init__(self):
            super().__init__()
            self.reads = 0
            self.both_read = asyncio.Event()

        async def read_by_operation(self, operation_id):
            self.reads += 1
            if self.reads == 2:
                self.both_read.set()
            await self.both_read.wait()
            return None

        async def create_batch(self, **kwargs):
            existing = next(iter(self.batches.values()), None)
            if existing:
                return {**existing, "_created": False}
            return await super().create_batch(**kwargs)

    journal = RaceJournal()
    queue = SimpleNamespace(
        enqueue_job=AsyncMock(
            side_effect=[
                QueueAdmission(AdmissionState.REJECTED, None, "job_capacity"),
                QueueAdmission(AdmissionState.ACCEPTED, "second"),
            ]
        )
    )
    service = BroadcastService(journal=journal, queue=queue)
    command = {"channel": "tarot", "user_ids": [101], "operation_id": "00000000-0000-4000-8000-000000000011"}
    results = await asyncio.gather(service.enqueue_offers(**command), service.enqueue_offers(**command))
    assert queue.enqueue_job.await_count == 1
    assert all(result["admission"]["state"] != "accepted" for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["trivia", "2048", "crocodile", "trivia_offer", "2048_offer"])
async def test_confirmed_photo_bookkeeping_failure_does_not_trigger_text_copy(game, monkeypatch):
    from datetime import date

    from app.handlers import daily_2048, daily_crocodile, daily_trivia

    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=42, chat_id=101)), send_message=AsyncMock()
    )
    journal = JournalProbe()
    broken = AsyncMock(side_effect=ConnectionError("synthetic post-send bookkeeping failure"))
    monkeypatch.setattr("app.games.cover_photo.remember_cover_file_id", broken)
    if game.startswith("trivia"):
        monkeypatch.setattr("app.games.cover_photo.get_cover_photo", AsyncMock(return_value="cover"))
        helper = (
            (lambda proxy: daily_trivia.send_discovery_intro(proxy, 101))
            if game.endswith("offer")
            else (
                lambda proxy: daily_trivia.send_daily_trivia_entry(proxy, 101, date(2097, 10, 28), mark_delivered=False)
            )
        )
    elif game.startswith("2048"):
        monkeypatch.setattr(daily_2048, "_get_cover_photo", AsyncMock(return_value="cover"))
        monkeypatch.setattr(daily_2048, "_remember_cover_file_id", broken)
        monkeypatch.setattr(daily_2048.repo, "ensure_puzzle", AsyncMock(return_value=SimpleNamespace(status="ready")))
        helper = (
            (lambda proxy: daily_2048.send_discovery_intro(proxy, 101))
            if game.endswith("offer")
            else (lambda proxy: daily_2048.send_daily2048_entry(proxy, 101, date(2097, 10, 28), mark_delivered=False))
        )
    else:
        monkeypatch.setattr(daily_crocodile, "_get_placeholder_file_id", AsyncMock(return_value="cover"))

        def helper(proxy):
            return daily_crocodile._send_daily_entry_message(
                proxy,
                chat_id=101,
                user_id=101,
                puzzle_date=date(2097, 10, 28),
                caption="synthetic",
                include_subscribe=False,
            )

    result = await owner(journal, bot).deliver(helper)
    assert result.outcome == "sent"
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert len(journal.attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed_slot", "sign", "offset", "time", "next_day", "already_sent"])
async def test_actual_horoscope_candidates_reject_removed_slot_with_other_slot_still_active(monkeypatch, change):
    from datetime import time

    from app.repos import broadcast_jobs as actual_repo

    row = {
        "user_id": 101,
        "memory_epoch": 7,
        "private_data_blocked": False,
        "daily_subscribed": False,
        "discovery_snoozed_until": None,
        "horoscope_subscribed": True,
        "tarot_subscribed": False,
        "observed_at": datetime(2097, 10, 28, 12, tzinfo=UTC),
        "sign": "aries",
        "utc_offset": 3,
        "time_today": time(9),
        "time_tomorrow": time(18),
        "last_today_sent": None,
        "last_tomorrow_sent": None,
    }

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace(fetch=AsyncMock(side_effect=lambda *args: [row.copy()]))

    monkeypatch.setattr(actual_repo, "transaction", transaction)

    class HoroscopeJournal(ExecutionJournal):
        async def candidates(self, channel, user_ids, **kwargs):
            return await actual_repo.candidates(channel, user_ids, **kwargs)

    journal = HoroscopeJournal()
    queue = SimpleNamespace(enqueue_job=AsyncMock(return_value=QueueAdmission(AdmissionState.ACCEPTED, "task")))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    service = BroadcastService(journal=journal, queue=queue, bot_provider=lambda: bot)
    result = await service._enqueue(
        channel="horoscope",
        user_ids=[101],
        operation_id="00000000-0000-4000-8000-000000000012",
        mode="delivery",
        variant="today",
    )
    if change == "removed_slot":
        row["time_today"] = None
    elif change == "sign":
        row["sign"] = "taurus"
    elif change == "offset":
        row["utc_offset"] = 4
    elif change == "time":
        row["time_today"] = time(10)
    elif change == "already_sent":
        row["last_today_sent"] = row["observed_at"]
    else:
        row["observed_at"] += timedelta(days=1)
    guard = SimpleNamespace(
        snapshot=SimpleNamespace(task_id="task"), checkpoint=AsyncMock(), update_progress=AsyncMock()
    )
    monkeypatch.setattr("app.queue.current_job_guard", lambda: guard)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", allowed_lease)
    monkeypatch.setattr(
        "app.repos.horoscope_subscriptions.get_horoscope_subscription",
        AsyncMock(return_value={"sign": "aries", "is_active": True, "time_today": None, "time_tomorrow": time(18)}),
    )
    monkeypatch.setattr(
        "app.intent_router._handle_horoscope", AsyncMock(return_value=SimpleNamespace(text="synthetic"))
    )
    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", AsyncMock(return_value="on"))
    await service.execute(result["batch"]["batch_id"])
    bot.send_message.assert_not_awaited()
    assert journal.batches[result["batch"]["batch_id"]]["skipped"] == 1


@pytest.mark.asyncio
async def test_stale_due_list_cannot_admit_a_horoscope_slot_moved_into_future(monkeypatch):
    from datetime import time

    from app.repos import broadcast_jobs as actual_repo

    row = {
        "user_id": 101,
        "memory_epoch": 7,
        "private_data_blocked": False,
        "daily_subscribed": False,
        "discovery_snoozed_until": None,
        "horoscope_subscribed": True,
        "tarot_subscribed": False,
        "observed_at": datetime(2097, 10, 28, 12, tzinfo=UTC),
        "sign": "aries",
        "utc_offset": 3,
        "time_today": time(18),
        "time_tomorrow": None,
        "last_today_sent": None,
        "last_tomorrow_sent": None,
    }

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace(fetch=AsyncMock(return_value=[row]))

    monkeypatch.setattr(actual_repo, "transaction", transaction)
    result = await actual_repo.candidates("horoscope", (101,), mode="delivery", variant="today")
    assert result[0]["reason_code"] == "slot_not_due"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_game", ["trivia", None])
async def test_daily_delivery_rechecks_explicit_and_default_game_preference(monkeypatch, selected_game):
    from app.repos import broadcast_jobs as actual_repo

    row = {
        "user_id": 101,
        "memory_epoch": 7,
        "private_data_blocked": False,
        "daily_subscribed": True,
        "daily_game": selected_game,
        "discovery_snoozed_until": None,
        "horoscope_subscribed": False,
        "tarot_subscribed": False,
        "observed_at": datetime(2097, 10, 28, 12, tzinfo=UTC),
    }

    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace(fetch=AsyncMock(return_value=[row]))

    monkeypatch.setattr(actual_repo, "transaction", transaction)
    monkeypatch.setattr("app.repos.daily_2048.get_active_daily_game_mode", AsyncMock(return_value="trivia"))
    result = await actual_repo.candidates("daily_challenge", (101,), mode="delivery", variant="crocodile")
    assert result[0]["reason_code"] == "game_preference_changed"
