"""Real journal delivery contracts; legacy claim helpers retain direct coverage.

Scheduler ticks now admit jobs. A partial/uncertain external send cannot be
retried by the next tick; receipts, privacy and ownership use real PostgreSQL.
Only queue admission and Telegram/provider transports are synthetic here.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.broadcast_jobs import BroadcastService
from app.handlers import scheduled_horoscopes as scheduler
from app.queue_contracts import AdmissionState, JobState, QueueAdmission
from app.repos import broadcast_jobs as journal
from app.repos import horoscope_subscriptions as subscriptions

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
USER = 86001


async def subscribe(connection, user=USER, *, tomorrow=False):
    await connection.execute("INSERT INTO users(user_id,is_authorized) VALUES($1,1)", user)
    await connection.execute("INSERT INTO chats(user_id) VALUES($1)", user)
    assert await subscriptions.upsert_horoscope_subscription(
        user,
        sign="aries",
        time_today="00:00",
        time_tomorrow="00:00" if tomorrow else None,
        utc_offset=0,
    )


@pytest.fixture
def service(monkeypatch):
    queue = SimpleNamespace(
        enqueue_job=AsyncMock(side_effect=lambda *a, **kw: QueueAdmission(AdmissionState.ACCEPTED, str(uuid4()))),
        read_job=AsyncMock(return_value=None),
        read_job_by_operation=AsyncMock(return_value=None),
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)))
    value = BroadcastService(journal=journal, queue=queue, bot_provider=lambda: bot)
    monkeypatch.setattr("app.broadcast_jobs.enqueue_scheduled", value.enqueue_scheduled)
    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", AsyncMock(return_value="on"))
    monkeypatch.setattr(
        "app.runtime_settings.lifecycle.operation_snapshot", AsyncMock(return_value=SimpleNamespace(revision=0))
    )
    monkeypatch.setattr(
        "app.intent_router._handle_horoscope", AsyncMock(return_value=SimpleNamespace(text="Synthetic horoscope"))
    )
    return value, queue, bot


async def enqueue(value, users=(USER,), kind="today"):
    results = await value.enqueue_scheduled(
        channel="horoscope",
        user_ids=list(users),
        effective_date=datetime.now(UTC).date(),
        variant=kind,
    )
    return results[0]["batch"]


async def execute(monkeypatch, value, batch):
    guard = SimpleNamespace(
        snapshot=SimpleNamespace(task_id=batch["task_id"]), checkpoint=AsyncMock(), update_progress=AsyncMock()
    )
    monkeypatch.setattr("app.queue.current_job_guard", lambda: guard)
    return await value.execute(str(batch["batch_id"]))


async def test_two_independent_scheduler_replicas_admit_one_delivery(db_conn, service, monkeypatch):
    await subscribe(db_conn)
    value, queue, bot = service
    original_due = scheduler.get_due_horoscope_subscriptions
    barrier = asyncio.Barrier(2)

    async def due(hour, minute, kind):
        rows = await original_due(hour, minute, kind)
        if kind == "today":
            assert [row["user_id"] for row in rows] == [USER]
            await barrier.wait()
        return rows

    monkeypatch.setattr(scheduler, "get_due_horoscope_subscriptions", due)
    await asyncio.wait_for(
        asyncio.gather(*[scheduler.check_and_send_horoscopes(SimpleNamespace(bot=bot)) for _ in range(2)]), 5
    )
    bot.send_message.assert_not_awaited()
    assert queue.enqueue_job.await_count == 1
    batches = await journal.open_batches()
    assert len(batches) == 1
    await execute(monkeypatch, value, batches[0])
    await execute(monkeypatch, value, batches[0])
    bot.send_message.assert_awaited_once()
    assert (await journal.read_batch(str(batches[0]["batch_id"])))["sent"] == 1
    assert await db_conn.fetchval(
        "SELECT last_today_sent IS NOT NULL FROM horoscope_subscriptions WHERE user_id=$1", USER
    )


async def test_partial_second_chunk_is_unknown_and_next_tick_does_not_replay(db_conn, service, monkeypatch):
    await subscribe(db_conn)
    value, queue, bot = service
    monkeypatch.setattr(
        "app.intent_router._handle_horoscope",
        AsyncMock(return_value=SimpleNamespace(text="A" * 3800 + "\n\n" + "B" * 200)),
    )
    bot.send_message.side_effect = [SimpleNamespace(message_id=501), ConnectionError("synthetic lost reply")]
    batch = await enqueue(value)
    await execute(monkeypatch, value, batch)
    assert bot.send_message.await_count == 2
    current = await journal.read_batch(batch["batch_id"])
    assert (current["unknown_delivery"], current["retryable_failed"]) == (1, 0)
    events, _ = await journal.list_events(channel="horoscope")
    assert sorted(event["outcome"] for event in events) == ["sent", "unknown_delivery"]
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=bot))
    await execute(monkeypatch, value, batch)
    assert queue.enqueue_job.await_count == 1
    assert bot.send_message.await_count == 2


async def test_preparation_failure_requires_explicit_child_retry(db_conn, service, monkeypatch):
    await subscribe(db_conn)
    value, queue, bot = service
    monkeypatch.setattr(
        "app.intent_router._handle_horoscope",
        AsyncMock(side_effect=[RuntimeError("synthetic generation failure"), SimpleNamespace(text="Recovered")]),
    )
    batch = await enqueue(value)
    await execute(monkeypatch, value, batch)
    bot.send_message.assert_not_awaited()
    assert (await journal.read_batch(batch["batch_id"]))["retryable_failed"] == 1
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=bot))
    assert queue.enqueue_job.await_count == 1
    child = await value.retry_failed(batch["batch_id"], operation_id=str(uuid4()))
    await execute(monkeypatch, value, child["batch"])
    bot.send_message.assert_awaited_once()
    assert (await journal.read_batch(batch["batch_id"]))["retryable_failed"] == 0


async def test_cancelled_external_send_remains_unknown_after_recovery(db_conn, service, monkeypatch):
    await subscribe(db_conn)
    value, queue, bot = service
    entered = asyncio.Event()

    async def sending(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    bot.send_message.side_effect = sending
    batch = await enqueue(value)
    task = asyncio.create_task(execute(monkeypatch, value, batch))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    queue.read_job.return_value = SimpleNamespace(
        state=JobState.CANCELLED, task_id=batch["task_id"], storage_mode="local"
    )
    await value.recover()
    row = await journal.read_batch(batch["batch_id"])
    assert row["state"] == "finished" and row["unknown_delivery"] == 1
    assert row["retryable_failed"] == 0
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=bot))
    assert bot.send_message.await_count == 1


@pytest.mark.parametrize("failure", ["mark", "commit"])
async def test_confirmed_receipt_survives_marker_failure_without_replay(db_conn, service, monkeypatch, failure):
    await subscribe(db_conn)
    value, queue, bot = service
    await db_conn.execute("""CREATE FUNCTION reject_horoscope_ack() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.last_today_sent IS NOT NULL THEN RAISE EXCEPTION 'synthetic acknowledgment failure';
        END IF; RETURN NEW; END $$""")
    ddl = (
        "CREATE TRIGGER reject_ack BEFORE UPDATE ON horoscope_subscriptions"
        if failure == "mark"
        else "CREATE CONSTRAINT TRIGGER reject_ack AFTER UPDATE ON horoscope_subscriptions DEFERRABLE INITIALLY DEFERRED"
    )
    await db_conn.execute(ddl + " FOR EACH ROW EXECUTE FUNCTION reject_horoscope_ack()")
    batch = await enqueue(value)
    await execute(monkeypatch, value, batch)
    bot.send_message.assert_awaited_once()
    row = await journal.read_batch(batch["batch_id"])
    assert (row["sent"], row["failed"], row["unknown_delivery"]) == (1, 0, 0)
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    await db_conn.execute("DROP TRIGGER reject_ack ON horoscope_subscriptions")
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=bot))
    await execute(monkeypatch, value, batch)
    bot.send_message.assert_awaited_once()
    assert queue.enqueue_job.await_count == 1


@pytest.mark.parametrize("change", ["disabled", "slot_removed", "already_sent", "future_slot"])
async def test_stale_due_list_is_rechecked_before_admission(db_conn, service, monkeypatch, change):
    await subscribe(db_conn, tomorrow=True)
    value, queue, bot = service
    original_due = scheduler.get_due_horoscope_subscriptions

    async def due(hour, minute, kind):
        if kind != "today":
            return []
        rows = await original_due(hour, minute, kind)
        assert rows
        mutations = {
            "disabled": "is_active=FALSE",
            "slot_removed": "time_today=NULL",
            "already_sent": "last_today_sent=clock_timestamp()",
            "future_slot": "time_today='23:59:59.999999'::time",
        }
        await db_conn.execute("UPDATE horoscope_subscriptions SET " + mutations[change])
        return rows

    monkeypatch.setattr(scheduler, "get_due_horoscope_subscriptions", due)
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=bot))
    batches = await journal.open_batches()
    assert len(batches) == 1 and batches[0]["skipped"] == 1
    await execute(monkeypatch, value, batches[0])
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("change", ["disabled", "slot", "sign", "offset"])
async def test_preferences_changed_during_send_cannot_acknowledge_new_subscription(
    db_conn, service, monkeypatch, change
):
    await subscribe(db_conn)
    value, _, bot = service
    mutations = {
        "disabled": "is_active=FALSE",
        "slot": "time_today=NULL",
        "sign": "sign='taurus'",
        "offset": "utc_offset=3",
    }

    async def send(**kwargs):
        await db_conn.execute("UPDATE horoscope_subscriptions SET " + mutations[change])
        return SimpleNamespace(message_id=501)

    bot.send_message.side_effect = send
    batch = await enqueue(value)
    await execute(monkeypatch, value, batch)
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    assert (await journal.read_batch(batch["batch_id"]))["sent"] == 1
    bot.send_message.assert_awaited_once()


async def test_five_parallel_recipients_are_bounded_and_do_not_hold_db_transactions(
    db_conn, service, contract_pool, monkeypatch
):
    for user in range(USER, USER + 6):
        await subscribe(db_conn, user)
    value, _, bot = service
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = calls = 0

    async def send(**kwargs):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        if active == 5:
            entered.set()
        try:
            await release.wait()
            return SimpleNamespace(message_id=500 + kwargs["chat_id"])
        finally:
            active -= 1

    bot.send_message.side_effect = send
    batch = await enqueue(value, range(USER, USER + 6))
    task = asyncio.create_task(execute(monkeypatch, value, batch))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert calls == peak == 5
        # This must succeed while all five external sends are blocked.
        async with contract_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout='500ms'")
                await conn.execute("UPDATE horoscope_subscriptions SET updated_at=clock_timestamp()")
    finally:
        release.set()
        await task
    assert calls == 6 and peak == 5
    assert (await journal.read_batch(batch["batch_id"]))["sent"] == 6


async def test_journal_ack_uses_pinned_claim_time_and_never_moves_marker_backward(db_conn):
    await subscribe(db_conn)
    await db_conn.execute("UPDATE horoscope_subscriptions SET utc_offset=3")
    fingerprint = journal.subscription_fingerprint({"sign": "aries", "utc_offset": 3, "time_today": time(0)}, "today")
    claimed_at = datetime(2026, 10, 9, 20, 59, tzinfo=UTC)
    recipient = {"delivery_claimed_at": claimed_at, "subscription_fingerprint": fingerprint}
    assert await journal.acknowledge_horoscope(USER, "today", recipient)
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") == claimed_at
    next_day = datetime(2026, 10, 9, 21, 1, tzinfo=UTC)
    assert await journal.acknowledge_horoscope(USER, "today", {**recipient, "delivery_claimed_at": next_day})
    assert await journal.acknowledge_horoscope(USER, "today", recipient)
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") == next_day


async def test_same_user_different_kinds_hold_independent_real_claims(db_conn):
    await subscribe(db_conn, tomorrow=True)
    barrier = asyncio.Barrier(2)
    pids = set()

    async def deliver(kind):
        async with subscriptions.claim_horoscope_delivery(USER, kind) as claim:
            assert claim is not None
            pids.add(claim._connection.get_server_pid())
            await barrier.wait()
            await claim.mark_sent()

    await asyncio.wait_for(asyncio.gather(deliver("today"), deliver("tomorrow")), 3)
    assert len(pids) == 2
    assert await db_conn.fetchval(
        "SELECT last_today_sent IS NOT NULL AND last_tomorrow_sent IS NOT NULL FROM horoscope_subscriptions"
    )


class ClockConnection:
    """Only control the DB-clock result; SQL, locks and transaction remain real."""

    def __init__(self, connection, clock):
        self.connection, self.clock = connection, clock

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def fetchval(self, query, *args, **kwargs):
        result = await self.connection.fetchval(query, *args, **kwargs)
        return self.clock[0] if query == "SELECT clock_timestamp()" else result


class ClockPool:
    def __init__(self, pool, clock):
        self.pool, self.clock = pool, clock

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as connection:
            yield ClockConnection(connection, self.clock)


async def test_midnight_send_marks_claim_day_and_next_local_day_remains_due(db_conn, contract_pool, monkeypatch):
    await subscribe(db_conn)
    await db_conn.execute("UPDATE horoscope_subscriptions SET utc_offset = 3")
    clock = [datetime(2026, 10, 9, 20, 59, tzinfo=UTC)]  # Local 23:59.
    monkeypatch.setattr(subscriptions.db_manager, "pool", ClockPool(contract_pool, clock))
    async with subscriptions.claim_horoscope_delivery(USER, "today") as claim:
        assert claim is not None
        backend_pid = claim._connection.get_server_pid()
        assert await claim._connection.fetchval("SHOW idle_in_transaction_session_timeout") == "3min"
        assert claim.claimed_at == clock[0]
        clock[0] = datetime(2026, 10, 9, 21, 1, tzinfo=UTC)  # Send completes local 00:01.
        await claim.mark_sent()
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") == datetime(
        2026, 10, 9, 20, 59, tzinfo=UTC
    )
    with pytest.raises(RuntimeError, match="no longer active"):
        await claim.mark_sent()
    async with subscriptions.claim_horoscope_delivery(USER, "today") as next_day:
        assert next_day is not None
        await next_day.mark_sent()
    async with subscriptions.claim_horoscope_delivery(USER, "today") as repeated:
        assert repeated is None
    async with contract_pool.acquire() as reused:
        assert reused.get_server_pid() == backend_pid
        assert await reused.fetchval("SHOW idle_in_transaction_session_timeout") != "3min"
