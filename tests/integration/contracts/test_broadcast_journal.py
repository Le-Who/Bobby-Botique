"""Real PostgreSQL commits, ownership fences, erasure and admin-only journal RLS."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from app.broadcast_delivery import DeliveryResult
from app.queue_contracts import AdmissionState, QueueAdmission
from app.repos import broadcast_jobs as repo

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_lost_begin_reconnects_without_replaying_transaction_body(
    contract_database_url, contract_pool, monkeypatch
):
    first_begin = True
    disconnected = []

    class DisconnectAtBegin(asyncpg.Connection):
        async def execute(self, query, *args, **kwargs):
            nonlocal first_begin
            if first_begin and query.strip() == "BEGIN;":
                first_begin = False
                backend = self.get_server_pid()
                marker = "SELECT pg_sleep(10) /* synthetic-broadcast-BEGIN */"

                async def disconnect_only_this_connection():
                    async with contract_pool.acquire() as controller:
                        async with asyncio.timeout(5):
                            while not await controller.fetchval(
                                "SELECT state='active' AND query=$2 FROM pg_stat_activity WHERE pid=$1", backend, marker
                            ):
                                await asyncio.sleep(0.01)
                        assert await controller.fetchval("SELECT pg_terminate_backend($1)", backend)
                        disconnected.append(backend)

                killer = asyncio.create_task(disconnect_only_this_connection())
                try:
                    return await super().execute(marker)
                finally:
                    await killer
            return await super().execute(query, *args, **kwargs)

    pool = await asyncpg.create_pool(
        contract_database_url, min_size=0, max_size=1, connection_class=DisconnectAtBegin, statement_cache_size=0
    )
    monkeypatch.setattr(repo.db.db_manager, "pool", pool)
    bodies = []
    try:
        async with repo.transaction() as connection:
            bodies.append(connection.get_server_pid())
            assert await connection.fetchval("SELECT current_setting('app.is_admin')") == "true"
            assert await connection.fetchval("SELECT 1") == 1
        assert len(disconnected) == len(bodies) == 1
        assert disconnected[0] != bodies[0]
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT NULLIF(current_setting('app.is_admin',true),'')") is None
    finally:
        await asyncio.wait_for(pool.close(), timeout=10)


async def make_batch(pool, *, users=(2097102801,), channel="tarot", mode="offer", scheduled=False, operation_id=None):
    async with pool.acquire() as conn:
        for user_id in users:
            await conn.execute(
                "INSERT INTO public.users(user_id,is_authorized) VALUES($1,1) ON CONFLICT DO NOTHING", user_id
            )
    return await repo.create_batch(
        batch_id=str(uuid4()),
        operation_id=str(operation_id or uuid4()),
        channel=channel,
        mode=mode,
        effective_date=date(2097, 10, 28),
        variant="today" if channel == "horoscope" else "",
        force=False,
        recipients=[{"recipient_id": user_id, "privacy_epoch": 7, "reason_code": None} for user_id in users],
        scheduled=scheduled,
    )


async def claim(batch):
    batch_token = uuid4()
    recipient_token = uuid4()
    batch_id = str(batch["batch_id"])
    assert await repo.claim_batch(batch_id, "queue-job", batch_token, settings_revision=42)
    assert await repo.claim_recipient(batch_id, 2097102801, batch_token, recipient_token)
    return batch_id, recipient_token


@pytest.mark.asyncio
async def test_restart_after_send_before_commit_is_unknown_and_late_reply_cannot_change_it(contract_pool):
    batch = await make_batch(contract_pool)
    batch_id, token = await claim(batch)
    attempt = await repo.start_attempt(batch_id, 2097102801, token, 1)
    # Telegram could have delivered here; simulate process death before its
    # response/commit. Recovery has no receipt and cannot infer no transmission.
    await repo.interrupt_batch(batch_id)
    assert not await repo.finish_attempt(
        attempt["attempt_id"], token, DeliveryResult("sent", "telegram_confirmed", message_id=501)
    )
    assert not await repo.finish_recipient(
        batch_id, 2097102801, token, DeliveryResult("sent", "telegram_confirmed", message_id=501)
    )
    row = await repo.read_batch(batch_id)
    assert row["unknown_delivery"] == 1 and row["state"] == "finished"
    assert await repo.recipients(batch_id, retryable_only=True) == []
    events, _ = await repo.list_events(channel="tarot", state="unknown_delivery")
    assert len(events) == 1 and events[0]["reason_code"] == "worker_interrupted"


@pytest.mark.asyncio
async def test_recovery_preserves_sent_entries_and_confirmation_message_id(contract_pool):
    batch = await make_batch(contract_pool)
    batch_id, token = await claim(batch)
    attempt = await repo.start_attempt(batch_id, 2097102801, token, 1)
    result = DeliveryResult("sent", "telegram_confirmed", message_id=502)
    assert await repo.finish_attempt(attempt["attempt_id"], token, result)
    assert await repo.finish_recipient(batch_id, 2097102801, token, result)
    await repo.interrupt_batch(batch_id)
    await repo.interrupt_batch(batch_id)
    row = await repo.read_batch(batch_id)
    assert (row["sent"], row["unknown_delivery"]) == (1, 0)
    events, _ = await repo.list_events(channel="tarot", state="sent")
    assert events[0]["message_id"] == 502 and events[0]["settings_revision"] == 42


@pytest.mark.asyncio
async def test_recipient_reservation_prevents_changed_due_list_from_resending_same_day(contract_pool):
    first = await make_batch(contract_pool, users=(2097102801, 2097102802), scheduled=True)
    second = await make_batch(contract_pool, users=(2097102802, 2097102803), scheduled=True)
    assert first["pending"] == 2
    assert (second["pending"], second["skipped"]) == (1, 1)
    assert [row["recipient_id"] for row in await repo.recipients(str(second["batch_id"]))] == [2097102803]


@pytest.mark.asyncio
async def test_global_journal_admission_cap_also_bounds_unconfirmed_drafts(contract_pool):
    batches = [await make_batch(contract_pool) for _ in range(10)]
    assert all(batches)
    assert await make_batch(contract_pool) is None
    await repo.record_admission(
        str(batches[0]["batch_id"]), QueueAdmission(AdmissionState.REJECTED, None, "job_capacity")
    )
    rejected = await repo.read_batch(str(batches[0]["batch_id"]))
    assert rejected["state"] == "finished" and rejected["retryable_failed"] == 1
    assert await make_batch(contract_pool) is not None


@pytest.mark.asyncio
async def test_account_delete_cascades_personal_journal_but_keeps_anonymous_aggregate(contract_pool):
    batch = await make_batch(contract_pool)
    batch_id, token = await claim(batch)
    attempt = await repo.start_attempt(batch_id, 2097102801, token, 1)
    result = DeliveryResult("sent", "telegram_confirmed", message_id=503)
    assert await repo.finish_attempt(attempt["attempt_id"], token, result)
    assert await repo.finish_recipient(batch_id, 2097102801, token, result)
    async with contract_pool.acquire() as conn:
        await conn.execute("DELETE FROM public.users WHERE user_id=$1", 2097102801)
        assert (
            await conn.fetchval("SELECT COUNT(*) FROM public.broadcast_recipients WHERE batch_id=$1", batch["batch_id"])
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM public.broadcast_delivery_attempts WHERE batch_id=$1", batch["batch_id"]
            )
            == 0
        )
    assert (await repo.read_batch(batch_id))["sent"] == 1


@pytest.mark.asyncio
async def test_personal_retention_30_days_does_not_remove_anonymous_aggregate(contract_pool):
    batch = await make_batch(contract_pool)
    async with contract_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.broadcast_recipients SET created_at=$1 WHERE batch_id=$2",
            datetime.now(UTC) - timedelta(days=31),
            batch["batch_id"],
        )
    await repo.purge_retained_recipients()
    assert await repo.recipients(str(batch["batch_id"])) == []
    assert await repo.read_batch(str(batch["batch_id"])) is not None


@pytest.mark.asyncio
async def test_retention_uses_index_bound_when_only_recent_recipients_exist(contract_pool, monkeypatch):
    async with contract_pool.acquire() as conn, conn.transaction():
        # Copy the real indexes without copying foreign keys or personal rows.
        await conn.execute(
            "CREATE TEMP TABLE retention_candidates "
            "(LIKE public.broadcast_recipients INCLUDING DEFAULTS INCLUDING INDEXES) ON COMMIT DROP"
        )
        await conn.execute(
            """INSERT INTO retention_candidates(batch_id,recipient_id,channel,privacy_epoch,created_at)
               SELECT $1,n,'tarot',7,statement_timestamp()-INTERVAL '1 day'
               FROM generate_series(1,10000) n""",
            uuid4(),
        )
        await conn.execute("ANALYZE retention_candidates")
        plans = []

        class RetentionConnection:
            async def execute(self, query):
                query = query.replace("public.broadcast_recipients", "pg_temp.retention_candidates")
                plans.append(json.loads(await conn.fetchval("EXPLAIN (FORMAT JSON) " + query))[0]["Plan"])
                return await conn.execute(query)

        @asynccontextmanager
        async def retention_transaction():
            yield RetentionConnection()

        monkeypatch.setattr(repo, "transaction", retention_transaction)
        assert await repo.purge_retained_recipients() == "DELETE 0"
        nodes = list(plans)
        indexed_cutoff = False
        while nodes:
            node = nodes.pop()
            indexed_cutoff |= "created_at" in node.get("Index Cond", "")
            nodes.extend(node.get("Plans", []))
        assert indexed_cutoff, "Retention must seek to its cutoff instead of filtering every recent row"
        assert await conn.fetchval("SELECT COUNT(*) FROM retention_candidates") == 10000


@pytest.mark.asyncio
async def test_offer_receipts_do_not_count_as_observed_tarot_readings(contract_pool):
    batch = await make_batch(contract_pool)
    batch_id, token = await claim(batch)
    assert await repo.finish_recipient(
        batch_id, 2097102801, token, DeliveryResult("sent", "telegram_confirmed", message_id=504)
    )
    assert (await repo.observed_counts("tarot", day=date(2097, 10, 28), mode="delivery"))["sent"] == 0
    assert (await repo.observed_counts("tarot", day=date(2097, 10, 28), mode="offer"))["sent"] == 1


@pytest.mark.asyncio
async def test_tarot_marker_requires_original_schedule_and_never_moves_backwards(contract_pool):
    from app.repos.tarot_daily_subscriptions import resolve_delivery_slot

    user_id = 2097102801
    claimed_at = datetime(2097, 10, 28, 12, tzinfo=UTC)
    subscription = {"timezone": "Europe/Kyiv", "preferred_local_hour": 10, "last_sent_date": None}
    slot = resolve_delivery_slot(subscription, claimed_at)
    recipient = {
        "slot_date": slot.slot_date,
        "delivery_claimed_at": claimed_at,
        "subscription_fingerprint": slot.subscription_fingerprint,
    }
    async with contract_pool.acquire() as conn:
        await conn.execute("INSERT INTO public.users(user_id,is_authorized) VALUES($1,1)", user_id)
        await conn.execute(
            """INSERT INTO public.tarot_daily_subscriptions(user_id,is_subscribed,timezone,preferred_local_hour)
            VALUES($1,TRUE,'Europe/Kyiv',10)""",
            user_id,
        )
    assert await repo.acknowledge_tarot(user_id, recipient)
    async with contract_pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT last_sent_date FROM public.tarot_daily_subscriptions WHERE user_id=$1", user_id)
            == slot.slot_date
        )
        await conn.execute(
            "UPDATE public.tarot_daily_subscriptions SET preferred_local_hour=11,last_sent_date=NULL WHERE user_id=$1",
            user_id,
        )
    assert not await repo.acknowledge_tarot(user_id, recipient)
    async with contract_pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT last_sent_date FROM public.tarot_daily_subscriptions WHERE user_id=$1", user_id)
            is None
        )
        await conn.execute(
            "UPDATE public.tarot_daily_subscriptions SET preferred_local_hour=10,last_sent_date=$2 WHERE user_id=$1",
            user_id,
            slot.slot_date + timedelta(days=1),
        )
    assert await repo.acknowledge_tarot(user_id, recipient)
    async with contract_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT last_sent_date FROM public.tarot_daily_subscriptions WHERE user_id=$1", user_id
        ) == slot.slot_date + timedelta(days=1)


@pytest.mark.asyncio
async def test_tarot_local_day_reservation_survives_content_day_and_schedule_changes(contract_pool):
    user_id = 2097102801
    async with contract_pool.acquire() as conn:
        await conn.execute("INSERT INTO public.users(user_id,is_authorized) VALUES($1,1)", user_id)
    batches = []
    for content_day, fingerprint in [(date(2097, 10, 27), "old-schedule"), (date(2097, 10, 28), "new-schedule")]:
        batches.append(
            await repo.create_batch(
                batch_id=str(uuid4()),
                operation_id=str(uuid4()),
                channel="tarot",
                mode="delivery",
                effective_date=content_day,
                variant="",
                force=False,
                scheduled=True,
                recipients=[
                    {
                        "recipient_id": user_id,
                        "privacy_epoch": 7,
                        "reason_code": None,
                        "slot_date": date(2097, 10, 28),
                        "subscription_fingerprint": fingerprint,
                        "delivery_claimed_at": datetime(2097, 10, 28, 12, tzinfo=UTC),
                    }
                ],
            )
        )
    assert batches[0]["pending"] == 1
    assert (batches[1]["pending"], batches[1]["skipped"]) == (0, 1)


@pytest.mark.asyncio
async def test_different_retry_operations_cannot_bypass_an_unconfirmed_child(contract_pool):
    parent = await make_batch(contract_pool, scheduled=True)
    parent_id, token = await claim(parent)
    assert await repo.finish_recipient(
        parent_id, 2097102801, token, DeliveryResult("failed", "telegram_rate_limited", True)
    )
    await repo.interrupt_batch(parent_id)

    async def child():
        return await repo.create_batch(
            batch_id=str(uuid4()),
            operation_id=str(uuid4()),
            channel="tarot",
            mode="offer",
            effective_date=date(2097, 10, 28),
            variant="",
            force=False,
            parent_batch_id=parent_id,
            recipients=[{"recipient_id": 2097102801, "privacy_epoch": 7, "reason_code": None}],
            scheduled=True,
        )

    first = await child()
    second = await child()
    assert first["pending"] == 1
    assert second["pending"] == 0 and second["skipped"] == 1
    assert (await repo.read_batch(parent_id))["retryable_failed"] == 0


@pytest.mark.asyncio
async def test_atomic_creator_result_has_only_one_admission_owner(contract_pool):
    operation = uuid4()
    batches = await asyncio.gather(
        make_batch(contract_pool, operation_id=operation), make_batch(contract_pool, operation_id=operation)
    )
    assert batches[0]["batch_id"] == batches[1]["batch_id"]
    assert sorted(batch["_created"] for batch in batches) == [False, True]


@pytest.mark.asyncio
async def test_all_journal_tables_deny_public_and_have_admin_only_rls(contract_pool):
    tables = ["broadcast_journal_coverage", "broadcast_batches", "broadcast_recipients", "broadcast_delivery_attempts"]
    async with contract_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT relname,relrowsecurity FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='public' AND relname=ANY($1::text[])",
            tables,
        )
        assert len(rows) == 4 and all(row["relrowsecurity"] for row in rows)
        for table in tables:
            assert not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.table_privileges WHERE table_schema='public' AND table_name=$1 AND grantee='PUBLIC')",
                table,
            )
            policy = await conn.fetchrow(
                "SELECT qual,with_check FROM pg_policies WHERE schemaname='public' AND tablename=$1", table
            )
            assert "app.is_admin" in policy["qual"] and "app.is_admin" in policy["with_check"]
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name=ANY($1::text[])", tables
        )
        assert not {"text", "body", "username", "response", "token"} & {row["column_name"] for row in columns}
