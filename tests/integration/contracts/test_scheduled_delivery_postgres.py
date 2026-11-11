"""Real-commit contracts for scheduled claims, receipts and source fencing."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.broadcast_delivery import DeliveryResult
from app.repos import scheduled_deliveries as repo

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
USER = 2097111101


async def prepare(pool, kind="reminder"):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO public.users(user_id) VALUES($1) ON CONFLICT DO NOTHING", USER)
        await conn.execute(
            "INSERT INTO public.chats(user_id,memory_epoch,ltm_enabled) VALUES($1,9001,TRUE) ON CONFLICT DO NOTHING",
            USER,
        )
        if kind == "reminder":
            return await conn.fetchval(
                """INSERT INTO public.user_reminders(user_id,trigger_at,prompt)
                   VALUES($1,clock_timestamp()-interval '1 minute','Synthetic reminder') RETURNING id""",
                USER,
            )
        return await conn.fetchrow(
            """INSERT INTO public.brief_subscriptions(user_id,preferred_hour)
               VALUES($1,$2) RETURNING *""",
            USER,
            datetime.now(UTC).hour,
        )


async def attempt(claim):
    return await repo.start_attempt(
        claim["delivery_id"],
        claim["user_id"],
        claim["owner_token"],
        claim["attempt_sequence"] + 1,
    )


async def expire(pool, claim):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.scheduled_deliveries SET expires_at=clock_timestamp()-interval '1 second' WHERE delivery_id=$1",
            claim["delivery_id"],
        )


async def test_two_pollers_get_one_reminder_claim(contract_pool):
    source = await prepare(contract_pool)
    claims = await asyncio.gather(repo.claim_reminder(source), repo.claim_reminder(source))
    assert len([claim for claim in claims if claim is not None]) == 1
    async with contract_pool.acquire() as conn:
        assert not await conn.fetchval("SELECT is_delivered FROM public.user_reminders WHERE id=$1", source)


async def test_receipt_settlement_and_marker_are_one_commit(contract_pool):
    source = await prepare(contract_pool)
    claim = await repo.claim_reminder(source)
    result = DeliveryResult("sent", "telegram_confirmed", message_id=71)
    started = await attempt(claim)
    assert await repo.finish_attempt(started["attempt_id"], claim["owner_token"], result)
    assert await repo.finish_delivery(claim, result)
    async with contract_pool.acquire() as conn:
        assert await conn.fetchval("SELECT is_delivered FROM public.user_reminders WHERE id=$1", source)
        assert (
            await conn.fetchval(
                "SELECT state FROM public.scheduled_deliveries WHERE delivery_id=$1",
                claim["delivery_id"],
            )
            == "sent"
        )
    assert not await repo.finish_delivery(claim, result)
    assert await repo.claim_reminder(source) is None


@pytest.mark.parametrize("receipt", [False, True])
async def test_restart_with_possible_effect_is_unknown_and_never_replays(contract_pool, receipt):
    source = await prepare(contract_pool)
    claim = await repo.claim_reminder(source)
    started = await attempt(claim)
    result = DeliveryResult("sent", "telegram_confirmed", message_id=71)
    if receipt:
        assert await repo.finish_attempt(started["attempt_id"], claim["owner_token"], result)
    await expire(contract_pool, claim)
    await repo.recover_abandoned()
    assert await repo.claim_reminder(source) is None
    assert not await repo.finish_attempt(started["attempt_id"], claim["owner_token"], result)
    assert not await repo.finish_delivery(claim, result)
    async with contract_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM public.scheduled_deliveries WHERE delivery_id=$1",
                claim["delivery_id"],
            )
            == "unknown_delivery"
        )
        assert not await conn.fetchval("SELECT is_delivered FROM public.user_reminders WHERE id=$1", source)


async def test_expired_preparation_can_recover_but_old_owner_cannot_send(contract_pool):
    source = await prepare(contract_pool)
    old = await repo.claim_reminder(source)
    await expire(contract_pool, old)
    await repo.recover_abandoned()
    fresh = await repo.claim_reminder(source)
    assert fresh and old["owner_token"] != fresh["owner_token"]
    assert not await repo.is_current(old)
    assert await attempt(old) is None
    assert await repo.is_current(fresh)


async def test_active_heartbeat_prevents_competing_recovery(contract_pool):
    source = await prepare(contract_pool)
    claim = await repo.claim_reminder(source)
    assert await repo.renew(claim)
    await repo.recover_abandoned()
    assert await repo.claim_reminder(source) is None
    assert await repo.is_current(claim)


async def test_safe_failures_retry_with_new_owner_sequence_and_bounded_budget(contract_pool):
    source = await prepare(contract_pool)
    result = DeliveryResult("failed", "telegram_rate_limited", True)
    previous = None
    for number in range(3):
        claim = await repo.claim_reminder(source)
        assert claim
        assert claim["attempt_sequence"] == number
        if previous is not None:
            assert previous["owner_token"] != claim["owner_token"]
            assert await attempt(previous) is None
        started = await attempt(claim)
        assert await repo.finish_attempt(started["attempt_id"], claim["owner_token"], result)
        assert await repo.finish_delivery(claim, result)
        assert await repo.claim_reminder(source) is None  # backoff
        async with contract_pool.acquire() as conn:
            await conn.execute(
                "UPDATE public.scheduled_deliveries SET retry_at=clock_timestamp()-interval '1 second' WHERE delivery_id=$1",
                claim["delivery_id"],
            )
        previous = claim
    assert await repo.claim_reminder(source) is None
    async with contract_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.scheduled_delivery_attempts WHERE delivery_id=$1",
                previous["delivery_id"],
            )
            == 3
        )


async def retryable_brief(pool):
    subscription = dict(await prepare(pool, "brief"))
    claim = await repo.claim_brief(USER, subscription=subscription)
    assert claim
    assert await repo.finish_delivery(claim, DeliveryResult("failed", "private_data_lease_unavailable", True))
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.scheduled_deliveries SET retry_at=clock_timestamp()-interval '1 second' WHERE delivery_id=$1",
            claim["delivery_id"],
        )
    retries = await repo.get_due_brief_retries()
    return claim, next(row for row in retries if row["delivery_id"] == claim["delivery_id"])


async def test_brief_retry_is_independent_of_initial_hour_and_keeps_exact_slot(contract_pool):
    from app.handlers.scheduled_briefs import get_due_subscriptions

    row = await prepare(contract_pool, "brief")
    async with contract_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.brief_subscriptions SET preferred_hour=(preferred_hour+23)%24 WHERE id=$1", row["id"]
        )
    # The initial execution has already been admitted in its preferred hour.
    # Its next persisted retry remains eligible in a later scheduler hour.
    original = await repo.claim_brief(USER)
    assert original
    assert await repo.finish_delivery(original, DeliveryResult("failed", "preparation_failed", True))
    async with contract_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.scheduled_deliveries SET retry_at=clock_timestamp()-interval '1 second' WHERE delivery_id=$1",
            original["delivery_id"],
        )
    assert not any(row["user_id"] == USER for row in await get_due_subscriptions(datetime.now(UTC).hour))
    retry = next(row for row in await repo.get_due_brief_retries() if row["delivery_id"] == original["delivery_id"])
    reclaimed = await repo.claim_brief_retry(retry)
    assert reclaimed["delivery_id"] == original["delivery_id"]
    assert reclaimed["slot_date"] == original["slot_date"]
    assert reclaimed["source_revision"] == original["source_revision"]
    assert reclaimed["privacy_epoch"] == original["privacy_epoch"]
    assert reclaimed["owner_token"] != original["owner_token"]
    assert not await repo.is_current(original)


async def test_two_pollers_cannot_reclaim_the_same_brief_retry(contract_pool):
    original, retry = await retryable_brief(contract_pool)
    claims = await asyncio.gather(repo.claim_brief_retry(retry), repo.claim_brief_retry(retry))
    assert len([claim for claim in claims if claim is not None]) == 1
    assert await attempt(original) is None


@pytest.mark.parametrize("mutation", ["revision", "epoch", "unknown_delivery", "expired_slot"])
async def test_captured_brief_retry_rechecks_source_and_quarantine(contract_pool, mutation):
    original, retry = await retryable_brief(contract_pool)
    async with contract_pool.acquire() as conn:
        if mutation == "revision":
            await conn.execute(
                "UPDATE public.brief_subscriptions SET delivery_revision=delivery_revision+1 WHERE id=$1",
                original["source_id"],
            )
        elif mutation == "epoch":
            await conn.execute("UPDATE public.chats SET memory_epoch=memory_epoch+1 WHERE user_id=$1", USER)
        elif mutation == "unknown_delivery":
            await conn.execute(
                "UPDATE public.scheduled_deliveries SET state='unknown_delivery' WHERE delivery_id=$1",
                original["delivery_id"],
            )
        else:
            await conn.execute(
                "UPDATE public.scheduled_deliveries SET slot_date=slot_date-1 WHERE delivery_id=$1",
                original["delivery_id"],
            )
    assert await repo.get_due_brief_retries() == []
    assert await repo.claim_brief_retry(retry) is None


async def test_brief_retries_preserve_the_bounded_failure_budget(contract_pool):
    claim, retry = await retryable_brief(contract_pool)
    for _ in range(2):
        claim = await repo.claim_brief_retry(retry)
        assert claim
        assert await repo.finish_delivery(claim, DeliveryResult("failed", "preparation_failed", True))
        async with contract_pool.acquire() as conn:
            await conn.execute(
                "UPDATE public.scheduled_deliveries SET retry_at=clock_timestamp()-interval '1 second' WHERE delivery_id=$1",
                claim["delivery_id"],
            )
        rows = await repo.get_due_brief_retries()
        if rows:
            retry = rows[0]
    assert rows == []
    assert await repo.claim_brief_retry(retry) is None


@pytest.mark.parametrize("mutation", ["epoch", "unsubscribe", "reschedule"])
async def test_brief_claim_is_invalidated_before_effect_and_ack(contract_pool, mutation):
    subscription = dict(await prepare(contract_pool, "brief"))
    claim = await repo.claim_brief(USER, subscription=subscription)
    assert claim and await repo.is_current(claim)
    result = DeliveryResult("sent", "telegram_confirmed", message_id=71)
    async with contract_pool.acquire() as conn:
        if mutation == "epoch":
            await conn.execute("UPDATE public.chats SET memory_epoch=9002 WHERE user_id=$1", USER)
        elif mutation == "unsubscribe":
            await conn.execute("UPDATE public.brief_subscriptions SET is_active=FALSE WHERE user_id=$1", USER)
        else:
            await conn.execute(
                "UPDATE public.brief_subscriptions SET preferred_hour=(preferred_hour+1)%24 WHERE user_id=$1", USER
            )
    assert not await repo.is_current(claim)
    assert await attempt(claim) is None
    assert not await repo.finish_delivery(claim, result)
    async with contract_pool.acquire() as conn:
        assert await conn.fetchval("SELECT last_sent_at FROM public.brief_subscriptions WHERE user_id=$1", USER) is None


async def test_subscription_revision_cannot_reuse_an_old_generation(contract_pool):
    row = await prepare(contract_pool, "brief")
    async with contract_pool.acquire() as conn:
        newer = await conn.fetchval(
            "UPDATE public.brief_subscriptions SET is_active=FALSE,delivery_revision=-1 WHERE id=$1 RETURNING delivery_revision",
            row["id"],
        )
        assert newer == row["delivery_revision"] + 1
        again = await conn.fetchval(
            "UPDATE public.brief_subscriptions SET is_active=TRUE,delivery_revision=$2 WHERE id=$1 RETURNING delivery_revision",
            row["id"],
            row["delivery_revision"],
        )
        assert again == newer + 1
    assert await repo.claim_brief(USER, subscription=dict(row)) is None


@pytest.mark.parametrize("effect", ["none", "unsettled", "sent", "unknown_delivery"])
async def test_schedule_change_preserves_daily_reservation_after_possible_effect(contract_pool, effect):
    row = await prepare(contract_pool, "brief")
    old = await repo.claim_brief(USER, subscription=dict(row))
    if effect != "none":
        started = await attempt(old)
        if effect != "unsettled":
            assert await repo.finish_attempt(
                started["attempt_id"],
                old["owner_token"],
                DeliveryResult(effect, "synthetic_effect", message_id=71),
            )
    async with contract_pool.acquire() as conn:
        newer = dict(
            await conn.fetchrow(
                "UPDATE public.brief_subscriptions SET delivery_revision=delivery_revision+1 WHERE id=$1 RETURNING *",
                row["id"],
            )
        )
    fresh = await repo.claim_brief(USER, subscription=newer)
    if effect == "none":
        assert fresh and fresh["source_revision"] == newer["delivery_revision"]
        assert fresh["owner_token"] != old["owner_token"]
        assert await attempt(old) is None
    else:
        assert fresh is None


async def test_due_poll_does_not_starve_after_fifty_quarantined_reminders(contract_pool):
    from app.repos.reminders import get_pending_reminders

    first = await prepare(contract_pool)
    ids = [first]
    async with contract_pool.acquire() as conn:
        ids += [
            row["id"]
            for row in await conn.fetch(
                """INSERT INTO public.user_reminders(user_id,trigger_at,prompt)
               SELECT $1,clock_timestamp()-interval '1 minute','Synthetic reminder' FROM generate_series(1,50)
               RETURNING id""",
                USER,
            )
        ]
    for source in ids[:50]:
        claim = await repo.claim_reminder(source)
        assert await repo.finish_delivery(claim, DeliveryResult("unknown_delivery", "worker_interrupted"))
    assert [row["id"] for row in await get_pending_reminders()] == [ids[-1]]


async def test_erasure_cascades_claims_and_attempts(contract_pool):
    source = await prepare(contract_pool)
    claim = await repo.claim_reminder(source)
    started = await attempt(claim)
    async with contract_pool.acquire() as conn:
        await conn.execute("DELETE FROM public.users WHERE user_id=$1", USER)
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.scheduled_deliveries WHERE delivery_id=$1", claim["delivery_id"]
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM public.scheduled_delivery_attempts WHERE attempt_id=$1", started["attempt_id"]
            )
            == 0
        )


async def test_migration_is_repeatable_and_keeps_existing_source_rows(contract_pool):
    source = await prepare(contract_pool)
    subscription = await prepare(contract_pool, "brief")
    sql = (Path(__file__).resolve().parents[3] / "scripts/migrations/082_scheduled_delivery_ownership.sql").read_text(
        encoding="utf-8"
    )
    async with contract_pool.acquire() as conn:
        for _ in range(2):
            await conn.execute(sql)
        assert (
            await conn.fetchval("SELECT prompt FROM public.user_reminders WHERE id=$1", source) == "Synthetic reminder"
        )
        assert (
            await conn.fetchval(
                "SELECT delivery_revision FROM public.brief_subscriptions WHERE id=$1", subscription["id"]
            )
            == subscription["delivery_revision"]
        )


async def test_journal_is_visible_only_under_admin_rls(contract_pool):
    source = await prepare(contract_pool)
    claim = await repo.claim_reminder(source)
    await attempt(claim)
    role = "scheduled_contract_" + uuid4().hex
    async with contract_pool.acquire() as conn:
        await conn.execute(f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOLOGIN')
        try:
            await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            await conn.execute(
                f'GRANT SELECT ON public.scheduled_deliveries,public.scheduled_delivery_attempts TO "{role}"'
            )
            async with conn.transaction():
                await conn.execute(f'SET LOCAL ROLE "{role}"')
                await conn.execute(
                    "SELECT set_config('app.user_id',$1,true),set_config('app.is_admin','false',true)", str(USER)
                )
                assert await conn.fetchval("SELECT count(*) FROM public.scheduled_deliveries") == 0
                assert await conn.fetchval("SELECT count(*) FROM public.scheduled_delivery_attempts") == 0
                await conn.execute("SELECT set_config('app.is_admin','true',true)")
                assert await conn.fetchval("SELECT count(*) FROM public.scheduled_deliveries") == 1
                assert await conn.fetchval("SELECT count(*) FROM public.scheduled_delivery_attempts") == 1
        finally:
            await conn.execute(f'DROP OWNED BY "{role}"')
            await conn.execute(f'DROP ROLE "{role}"')
