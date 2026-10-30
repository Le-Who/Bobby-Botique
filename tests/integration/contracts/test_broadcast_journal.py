"""Real PostgreSQL commits, ownership fences, erasure and admin-only journal RLS."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.broadcast_delivery import DeliveryResult
from app.queue_contracts import AdmissionState, QueueAdmission
from app.repos import broadcast_jobs as repo

pytestmark = pytest.mark.integration


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
async def test_offer_receipts_do_not_count_as_observed_tarot_readings(contract_pool):
    batch = await make_batch(contract_pool)
    batch_id, token = await claim(batch)
    assert await repo.finish_recipient(
        batch_id, 2097102801, token, DeliveryResult("sent", "telegram_confirmed", message_id=504)
    )
    assert (await repo.observed_counts("tarot", day=date(2097, 10, 28), mode="delivery"))["sent"] == 0
    assert (await repo.observed_counts("tarot", day=date(2097, 10, 28), mode="offer"))["sent"] == 1


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
