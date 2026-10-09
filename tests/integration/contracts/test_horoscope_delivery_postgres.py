"""product-05: scheduled chunk retry and independent replica delivery claims."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.handlers import scheduled_horoscopes as scheduler
from app.repos import horoscope_subscriptions as subscriptions

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
USER = 86001


@pytest.fixture
def enabled(monkeypatch):
    async def on(*args):
        return "on"

    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", on)


async def subscribe(connection, user=USER, *, tomorrow=False):
    await connection.execute("INSERT INTO users(user_id) VALUES($1)", user)
    assert await subscriptions.upsert_horoscope_subscription(
        user,
        sign="aries",
        time_today="00:00",
        time_tomorrow="00:00" if tomorrow else None,
        utc_offset=0,
    )


async def test_two_independent_scheduler_replicas_send_one_delivery(db_conn, enabled, monkeypatch):
    await subscribe(db_conn)
    original_due = scheduler.get_due_horoscope_subscriptions
    barrier = asyncio.Barrier(2)

    async def due(hour, minute, kind):
        rows = await original_due(hour, minute, kind)
        if kind == "today":
            assert [row["user_id"] for row in rows] == [USER]
            await barrier.wait()
        return rows

    async def generate(*args):
        return SimpleNamespace(text="Controlled daily horoscope")

    sent, backend_pids = [], set()
    loser_finished = asyncio.Event()
    original_context = subscriptions.set_user_context

    async def record_connection(*args, **kwargs):
        backend_pids.add(kwargs["conn"].get_server_pid())
        await original_context(*args, **kwargs)

    @asynccontextmanager
    async def claim(user_id, kind):
        async with subscriptions.claim_horoscope_delivery(user_id, kind) as owned:
            if owned is None:
                loser_finished.set()
            yield owned

    async def send(**kwargs):
        sent.append(kwargs)
        await asyncio.wait_for(loser_finished.wait(), 3)

    monkeypatch.setattr(scheduler, "get_due_horoscope_subscriptions", due)
    monkeypatch.setattr(subscriptions, "set_user_context", record_connection)
    monkeypatch.setattr(scheduler, "claim_horoscope_delivery", claim)
    monkeypatch.setattr("app.intent_router._handle_horoscope", generate)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send))
    await asyncio.wait_for(
        asyncio.gather(scheduler.check_and_send_horoscopes(context), scheduler.check_and_send_horoscopes(context)), 5
    )
    assert len(sent) == 1
    assert len(backend_pids) == 2
    assert await db_conn.fetchval(
        "SELECT last_today_sent IS NOT NULL FROM horoscope_subscriptions WHERE user_id=$1", USER
    )
    monkeypatch.setattr(scheduler, "get_due_horoscope_subscriptions", original_due)
    await scheduler.check_and_send_horoscopes(context)
    assert len(sent) == 1


async def test_partial_second_chunk_failure_retries_complete_message_next_tick(db_conn, enabled, monkeypatch):
    await subscribe(db_conn)
    # Hand-sized paragraphs: header + 3,800 A's fits 3,900; B paragraph starts chunk two.
    first = "🌅 <b>Ваш гороскоп на сегодня</b> (♈ Овен):\n\n" + "A" * 3800 + "\n\n"
    second = "B" * 200

    async def generate(*args):
        return SimpleNamespace(text="A" * 3800 + "\n\n" + second)

    attempts, delivered = [], []
    fail = True

    async def send(**kwargs):
        nonlocal fail
        attempts.append(kwargs["text"])
        if fail and len(attempts) == 2:
            fail = False
            raise RuntimeError("controlled chunk-two failure")
        delivered.append(kwargs)

    monkeypatch.setattr("app.intent_router._handle_horoscope", generate)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send))
    await scheduler.check_and_send_horoscopes(context)
    assert attempts == [first, second]
    assert [chunk["text"] for chunk in delivered] == [first]
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    await scheduler.check_and_send_horoscopes(context)
    assert attempts == [first, second, first, second]
    assert [chunk["text"] for chunk in delivered] == [first, first, second]
    assert delivered[0]["reply_markup"] is None and delivered[1]["reply_markup"] is None
    assert delivered[2]["reply_markup"] is not None
    assert await db_conn.fetchval("SELECT last_today_sent IS NOT NULL FROM horoscope_subscriptions")
    await scheduler.check_and_send_horoscopes(context)
    assert attempts == [first, second, first, second]


@pytest.mark.parametrize("failure", ["false", "exception", "cancel"])
async def test_failed_or_cancelled_delivery_releases_claim_for_retry(db_conn, enabled, monkeypatch, failure):
    await subscribe(db_conn)
    entered, cleaned = asyncio.Event(), asyncio.Event()
    calls = 0

    async def deliver(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            return True
        if failure == "false":
            return False
        if failure == "exception":
            raise RuntimeError("controlled delivery failure")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(scheduler, "_deliver_horoscope", deliver)
    context = SimpleNamespace(bot=object())
    if failure == "cancel":
        tick = asyncio.create_task(scheduler.check_and_send_horoscopes(context))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            tick.cancel()
            with pytest.raises(asyncio.CancelledError):
                await tick
            assert cleaned.is_set()
        finally:
            if not tick.done():
                tick.cancel()
                await asyncio.gather(tick, return_exceptions=True)
    else:
        await scheduler.check_and_send_horoscopes(context)
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    await asyncio.wait_for(scheduler.check_and_send_horoscopes(context), 3)
    assert calls == 2
    assert await db_conn.fetchval("SELECT last_today_sent IS NOT NULL FROM horoscope_subscriptions")


@pytest.mark.parametrize("failure", ["mark", "commit"])
async def test_real_mark_or_commit_failure_rolls_back_and_never_logs_success(
    db_conn, enabled, monkeypatch, caplog, failure
):
    await subscribe(db_conn)
    await db_conn.execute(
        """CREATE FUNCTION reject_horoscope_ack() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN IF NEW.last_today_sent IS NOT NULL THEN
             RAISE EXCEPTION 'controlled acknowledgment failure';
           END IF; RETURN NEW; END $$"""
    )
    if failure == "mark":
        ddl = "CREATE TRIGGER reject_ack BEFORE UPDATE ON horoscope_subscriptions"
    else:
        ddl = (
            "CREATE CONSTRAINT TRIGGER reject_ack AFTER UPDATE ON horoscope_subscriptions DEFERRABLE INITIALLY DEFERRED"
        )
    await db_conn.execute(ddl + " FOR EACH ROW EXECUTE FUNCTION reject_horoscope_ack()")
    sent = []

    async def generate(*args):
        return SimpleNamespace(text="Controlled acknowledgment test")

    async def send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("app.intent_router._handle_horoscope", generate)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send))
    caplog.set_level(logging.INFO, logger=scheduler.__name__)
    await scheduler.check_and_send_horoscopes(context)
    assert len(sent) == 1
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    assert not any("delivered to" in record.message for record in caplog.records)
    assert any("delivery task failed" in record.message for record in caplog.records)
    await db_conn.execute("DROP TRIGGER reject_ack ON horoscope_subscriptions")
    await scheduler.check_and_send_horoscopes(context)
    assert len(sent) == 2  # External send succeeded before the acknowledgment failed.
    assert await db_conn.fetchval("SELECT last_today_sent IS NOT NULL FROM horoscope_subscriptions")
    assert sum("delivered to" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("change", ["disabled", "slot_removed", "already_sent", "future_slot"])
async def test_stale_due_snapshot_is_rechecked_in_claim(db_conn, enabled, monkeypatch, change):
    await subscribe(db_conn)
    original_due = scheduler.get_due_horoscope_subscriptions

    async def due(hour, minute, kind):
        rows = await original_due(hour, minute, kind)
        if kind == "today":
            assert len(rows) == 1
            mutations = {
                "disabled": "is_active = FALSE",
                "slot_removed": "time_today = NULL",
                "already_sent": "last_today_sent = clock_timestamp()",
                "future_slot": "time_today = '23:59:59.999999'::time",
            }
            await db_conn.execute("UPDATE horoscope_subscriptions SET " + mutations[change])
        return rows

    deliver = []

    async def send(*args):
        deliver.append(args)
        return True

    monkeypatch.setattr(scheduler, "get_due_horoscope_subscriptions", due)
    monkeypatch.setattr(scheduler, "_deliver_horoscope", send)
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=object()))
    assert deliver == []


async def test_claim_uses_current_preferences_and_independent_kinds(db_conn, enabled, monkeypatch):
    await subscribe(db_conn, tomorrow=True)
    original_due = scheduler.get_due_horoscope_subscriptions
    sent = []

    async def due(hour, minute, kind):
        rows = await original_due(hour, minute, kind)
        if kind == "today":
            assert rows[0]["sign"] == "aries"
            await db_conn.execute("UPDATE horoscope_subscriptions SET sign = 'taurus', utc_offset = 3")
        return rows

    async def deliver(_bot, user_id, sign, kind):
        sent.append((user_id, sign, kind))
        return True

    monkeypatch.setattr(scheduler, "get_due_horoscope_subscriptions", due)
    monkeypatch.setattr(scheduler, "_deliver_horoscope", deliver)
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=object()))
    assert sent == [(USER, "taurus", "today"), (USER, "taurus", "tomorrow")]
    assert await db_conn.fetchval(
        "SELECT last_today_sent IS NOT NULL AND last_tomorrow_sent IS NOT NULL FROM horoscope_subscriptions"
    )


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


@pytest.mark.parametrize("change", ["disabled", "slot", "sign", "offset"])
async def test_changed_preferences_during_send_cannot_acknowledge_new_subscription(
    db_conn, enabled, monkeypatch, caplog, change
):
    await subscribe(db_conn)
    mutations = {
        "disabled": "is_active=FALSE",
        "slot": "time_today=NULL",
        "sign": "sign='taurus'",
        "offset": "utc_offset=3",
    }

    async def deliver(*args):
        await db_conn.execute("UPDATE horoscope_subscriptions SET " + mutations[change])
        return True

    monkeypatch.setattr(scheduler, "_deliver_horoscope", deliver)
    caplog.set_level(logging.INFO, logger=scheduler.__name__)
    await scheduler.check_and_send_horoscopes(SimpleNamespace(bot=object()))
    assert await db_conn.fetchval("SELECT last_today_sent FROM horoscope_subscriptions") is None
    assert not any("delivered to" in record.message for record in caplog.records)
    assert any("delivery task failed" in record.message for record in caplog.records)


async def test_four_parallel_real_claims_bound_sends_without_blocking_other_recipients(db_conn, enabled, monkeypatch):
    for user in range(USER, USER + 5):
        await subscribe(db_conn, user)
    entered, release = asyncio.Event(), asyncio.Event()
    active, peak, calls = 0, 0, 0

    async def deliver(*args):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        if active == 4:
            entered.set()
        try:
            await release.wait()
            return True
        finally:
            active -= 1

    monkeypatch.setattr(scheduler, "_deliver_horoscope", deliver)
    tick = asyncio.create_task(scheduler.check_and_send_horoscopes(SimpleNamespace(bot=object())))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert calls == 4 and peak == 4
    finally:
        release.set()
        await tick
    assert calls == 5 and peak == 4
    assert await db_conn.fetchval("SELECT count(*) FROM horoscope_subscriptions WHERE last_today_sent IS NOT NULL") == 5


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
