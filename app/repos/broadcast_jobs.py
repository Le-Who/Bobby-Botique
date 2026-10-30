"""Short database transactions for broadcast ownership and receipt observations."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import date, timedelta
from uuid import UUID, uuid4, uuid5

from app import database as db

CHANNELS = frozenset({"daily_challenge", "horoscope", "tarot"})
OUTCOMES = frozenset({"sent", "failed", "skipped", "unknown_delivery"})


@asynccontextmanager
async def transaction(conn=None):
    if conn is not None:
        yield conn
        return
    if db.db_manager.pool is None:
        raise RuntimeError("broadcast_database_unavailable")
    async with db.db_manager.pool.acquire() as acquired, acquired.transaction():
        await db.set_user_context(0, True, conn=acquired)
        try:
            yield acquired
        finally:
            await db.clear_user_context(conn=acquired)


def subscription_fingerprint(row, variant):
    slot = row.get("time_" + variant)
    if slot is None:
        return None
    return hashlib.sha256(f"{row['sign']}:{row['utc_offset']}:{slot}".encode()).hexdigest()


async def candidates(channel: str, user_ids: tuple[int, ...], *, mode: str, force: bool = False, variant=""):
    # No username, birth data, message body or provider output crosses this seam.
    async with transaction() as conn:
        rows = await conn.fetch(
            """SELECT u.user_id,c.memory_epoch,c.private_data_blocked,
               COALESCE(d.is_subscribed,FALSE) AS daily_subscribed,
               d.discovery_snoozed_until,d.daily_game,
               COALESCE(h.is_active,FALSE) AS horoscope_subscribed,
               h.sign,h.utc_offset,h.time_today,h.time_tomorrow,h.last_today_sent,h.last_tomorrow_sent,
               COALESCE(t.is_subscribed,FALSE) AS tarot_subscribed,
               clock_timestamp() AS observed_at
               FROM public.users u LEFT JOIN public.chats c USING(user_id)
               LEFT JOIN public.crocodile_daily_preferences d USING(user_id)
               LEFT JOIN public.horoscope_subscriptions h USING(user_id)
               LEFT JOIN public.tarot_daily_subscriptions t USING(user_id)
               WHERE u.user_id=ANY($1::bigint[])""",
            list(user_ids),
        )
    by_id = {r["user_id"]: r for r in rows}
    default_game = None
    if channel == "daily_challenge" and mode == "delivery" and any(row["daily_game"] is None for row in rows):
        from app.repos.daily_2048 import get_active_daily_game_mode

        default_game = await get_active_daily_game_mode()
    result = []
    field = {"daily_challenge": "daily_subscribed", "horoscope": "horoscope_subscribed", "tarot": "tarot_subscribed"}[
        channel
    ]
    for user_id in user_ids:
        row = by_id.get(user_id)
        reason = None
        if row is None:
            reason = "account_missing"
        elif row["memory_epoch"] is None or row["private_data_blocked"]:
            reason = "privacy_unavailable"
        elif mode == "delivery" and not row[field]:
            reason = "unsubscribed"
        elif mode == "delivery" and channel == "daily_challenge" and (row["daily_game"] or default_game) != variant:
            reason = "game_preference_changed"
        elif mode == "delivery" and channel == "horoscope":
            slot = row["time_" + variant]
            offset = timedelta(hours=row["utc_offset"] or 0)
            local_now = row["observed_at"] + offset
            last_sent = row.get("last_" + variant + "_sent")
            if slot is None:
                reason = "slot_unsubscribed"
            elif slot > local_now.time():
                reason = "slot_not_due"
            elif last_sent is not None and (last_sent + offset).date() >= local_now.date():
                reason = "already_delivered"
        elif mode == "offer" and row[field] and not force:
            reason = "already_subscribed"
        elif (
            mode == "offer"
            and channel == "daily_challenge"
            and row["discovery_snoozed_until"]
            and row["discovery_snoozed_until"] > row["observed_at"]
        ):
            reason = "discovery_snoozed"
        snapshot = {
            "recipient_id": user_id,
            "privacy_epoch": row["memory_epoch"] if row else None,
            "reason_code": reason,
        }
        if channel == "horoscope" and mode == "delivery" and row is not None:
            snapshot.update(
                subscription_fingerprint=subscription_fingerprint(dict(row), variant),
                delivery_claimed_at=row["observed_at"],
                slot_date=(row["observed_at"] + timedelta(hours=row["utc_offset"] or 0)).date(),
            )
        result.append(snapshot)
    return result


async def read_batch(batch_id):
    async with transaction() as conn:
        row = await conn.fetchrow("SELECT * FROM public.broadcast_batches WHERE batch_id=$1", UUID(str(batch_id)))
        return dict(row) if row else None


async def read_by_operation(operation_id):
    async with transaction() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM public.broadcast_batches WHERE operation_id=$1", UUID(str(operation_id))
        )
        return dict(row) if row else None


async def create_batch(
    *,
    batch_id,
    operation_id,
    channel,
    mode,
    effective_date,
    variant,
    force,
    recipients,
    parent_batch_id=None,
    settings_revision=None,
    scheduled=False,
):
    async with transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('gemaibotv2:broadcast:admission',0))")
        existing = await conn.fetchrow(
            "SELECT * FROM public.broadcast_batches WHERE operation_id=$1", UUID(str(operation_id))
        )
        if existing:
            return {**dict(existing), "_created": False}
        if await conn.fetchval("SELECT COUNT(*) FROM public.broadcast_batches WHERE state<>'finished'") >= 10:
            return None
        # One failed recipient grants one child, even if two tabs use different
        # operation IDs. An unknown child cannot be bypassed through its parent.
        if parent_batch_id:
            consumed = 0
            for recipient in recipients:
                source = await conn.fetchval(
                    """UPDATE public.broadcast_recipients SET retryable=FALSE
                    WHERE batch_id=$1 AND recipient_id=$2 AND state='failed' AND retryable RETURNING recipient_id""",
                    UUID(str(parent_batch_id)),
                    recipient["recipient_id"],
                )
                if source is not None:
                    consumed += 1
                elif recipient["reason_code"] is None:
                    recipient["reason_code"] = "retry_already_requested"
            await conn.execute(
                "UPDATE public.broadcast_batches SET retryable_failed=retryable_failed-$2 WHERE batch_id=$1",
                UUID(str(parent_batch_id)),
                consumed,
            )
        eligible = [r for r in recipients if r["reason_code"] is None]
        row = await conn.fetchrow(
            """INSERT INTO public.broadcast_batches(batch_id,operation_id,parent_batch_id,channel,mode,
               effective_date,variant,force_offer,settings_revision,total,pending,skipped,scheduled)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT(operation_id) DO NOTHING RETURNING *""",
            UUID(str(batch_id)),
            UUID(str(operation_id)),
            UUID(str(parent_batch_id)) if parent_batch_id else None,
            channel,
            mode,
            effective_date,
            variant,
            force,
            settings_revision,
            len(recipients),
            len(eligible),
            len(recipients) - len(eligible),
            scheduled,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM public.broadcast_batches WHERE operation_id=$1", UUID(str(operation_id))
            )
            return {**dict(row), "_created": False}
        for recipient in eligible:
            schedule_key = (
                hashlib.sha256(
                    f"{channel}:{mode}:{recipient.get('slot_date') or effective_date}:{variant}:{recipient['recipient_id']}:{recipient['privacy_epoch']}".encode()
                ).hexdigest()
                if scheduled
                else None
            )
            if parent_batch_id and schedule_key:
                await conn.execute(
                    """UPDATE public.broadcast_recipients SET schedule_key=NULL
                    WHERE batch_id=$1 AND recipient_id=$2 AND state='failed' AND schedule_key=$3""",
                    UUID(str(parent_batch_id)),
                    recipient["recipient_id"],
                    schedule_key,
                )
            # Epoch remains the captured original even if erasure/recreation raced
            # this insert. The live lease/checkpoint will reject that old epoch.
            inserted = await conn.fetchval(
                """INSERT INTO public.broadcast_recipients(batch_id,recipient_id,channel,privacy_epoch,schedule_key,subscription_fingerprint,delivery_claimed_at,slot_date)
                   SELECT $1,$2,$3,$4,$5,$6,$7,$8 WHERE EXISTS(SELECT 1 FROM public.users WHERE user_id=$2)
                   ON CONFLICT DO NOTHING RETURNING recipient_id""",
                UUID(str(batch_id)),
                recipient["recipient_id"],
                channel,
                recipient["privacy_epoch"],
                schedule_key,
                recipient.get("subscription_fingerprint"),
                recipient.get("delivery_claimed_at"),
                recipient.get("slot_date"),
            )
            if inserted is None:
                await conn.execute(
                    "UPDATE public.broadcast_batches SET pending=pending-1,skipped=skipped+1 WHERE batch_id=$1",
                    UUID(str(batch_id)),
                )
        return {
            **dict(
                await conn.fetchrow("SELECT * FROM public.broadcast_batches WHERE batch_id=$1", UUID(str(batch_id)))
            ),
            "_created": True,
        }


async def record_admission(batch_id, admission):
    async with transaction() as conn:
        await conn.execute(
            """UPDATE public.broadcast_batches SET admission_state=$2,admission_reason=$3,
            storage_mode=$4,task_id=COALESCE(task_id,$5),state=CASE WHEN state='draft' AND $5::text IS NOT NULL THEN 'queued' ELSE state END
            WHERE batch_id=$1""",
            UUID(str(batch_id)),
            str(admission.state),
            admission.reason_code or "admission_confirmed",
            admission.storage_mode,
            admission.task_id,
        )
        if str(admission.state) == "rejected":
            await conn.execute(
                """UPDATE public.broadcast_recipients SET state='failed',reason_code='queue_admission_rejected',retryable=TRUE
                WHERE batch_id=$1 AND state='pending'""",
                UUID(str(batch_id)),
            )
            await conn.execute(
                """UPDATE public.broadcast_batches SET failed=failed+pending,retryable_failed=retryable_failed+pending,
                pending=0,state='finished',finished_at=clock_timestamp() WHERE batch_id=$1 AND state IN ('draft','queued')""",
                UUID(str(batch_id)),
            )


async def claim_batch(batch_id, task_id, token, *, settings_revision=None):
    async with transaction() as conn:
        row = await conn.fetchrow(
            """UPDATE public.broadcast_batches SET state='running',owner_token=$3,settings_revision=$4
               WHERE batch_id=$1 AND (task_id IS NULL OR task_id=$2) AND state IN ('draft','queued')
               RETURNING *""",
            UUID(str(batch_id)),
            task_id,
            UUID(str(token)),
            settings_revision,
        )
        if row:
            await conn.execute(
                "UPDATE public.broadcast_batches SET task_id=$2 WHERE batch_id=$1", UUID(str(batch_id)), task_id
            )
        return dict(row) if row else None


async def recipients(batch_id, *, retryable_only=False):
    condition = "state='failed' AND retryable" if retryable_only else "state='pending'"
    async with transaction() as conn:
        rows = await conn.fetch(
            f"SELECT recipient_id,privacy_epoch,subscription_fingerprint,delivery_claimed_at,slot_date FROM public.broadcast_recipients WHERE batch_id=$1 AND {condition} ORDER BY recipient_id LIMIT 100",
            UUID(str(batch_id)),
        )
        return [dict(row) for row in rows]


async def claim_recipient(batch_id, recipient_id, batch_token, token):
    async with transaction() as conn:
        row = await conn.fetchrow(
            """UPDATE public.broadcast_recipients r SET state='started',owner_token=$4
               FROM public.broadcast_batches b WHERE r.batch_id=$1 AND r.recipient_id=$2
               AND b.batch_id=r.batch_id AND b.owner_token=$3 AND b.state='running' AND r.state='pending'
               RETURNING r.recipient_id""",
            UUID(str(batch_id)),
            recipient_id,
            UUID(str(batch_token)),
            UUID(str(token)),
        )
        if row:
            await conn.execute(
                "UPDATE public.broadcast_batches SET pending=pending-1,started=started+1 WHERE batch_id=$1",
                UUID(str(batch_id)),
            )
        return row is not None


async def start_attempt(batch_id, recipient_id, token, sequence):
    attempt_id = uuid4()
    async with transaction() as conn:
        row = await conn.fetchrow(
            """INSERT INTO public.broadcast_delivery_attempts(attempt_id,batch_id,recipient_id,channel,sequence,
               operation_id,owner_token,effective_date,settings_revision)
               SELECT $1,r.batch_id,r.recipient_id,r.channel,$4,$5,r.owner_token,COALESCE(r.slot_date,b.effective_date),b.settings_revision
               FROM public.broadcast_recipients r JOIN public.broadcast_batches b USING(batch_id)
               WHERE r.batch_id=$2 AND r.recipient_id=$3 AND r.state='started' AND r.owner_token=$6
               RETURNING attempt_id""",
            attempt_id,
            UUID(str(batch_id)),
            recipient_id,
            sequence,
            uuid5(UUID(str(batch_id)), f"{recipient_id}:{sequence}"),
            UUID(str(token)),
        )
        return {"attempt_id": str(row["attempt_id"])} if row else None


async def finish_attempt(attempt_id, token, result):
    async with transaction() as conn:
        row = await conn.fetchrow(
            """UPDATE public.broadcast_delivery_attempts a SET outcome=$3,reason_code=$4,retryable=$5,
               message_id=$6,finished_at=clock_timestamp() FROM public.broadcast_recipients r
               WHERE a.attempt_id=$1 AND a.owner_token=$2 AND a.outcome='started'
               AND r.batch_id=a.batch_id AND r.recipient_id=a.recipient_id AND r.channel=a.channel
               AND r.state='started' AND r.owner_token=$2 RETURNING a.attempt_id""",
            UUID(str(attempt_id)),
            UUID(str(token)),
            result.outcome,
            result.reason_code,
            result.retryable,
            result.message_id,
        )
        return row is not None


async def finish_recipient(batch_id, recipient_id, token, result):
    if result.outcome not in OUTCOMES:
        raise ValueError("invalid_delivery_outcome")
    async with transaction() as conn:
        row = await conn.fetchrow(
            """UPDATE public.broadcast_recipients SET state=$4,reason_code=$5,retryable=$6,message_id=$7,owner_token=NULL
               WHERE batch_id=$1 AND recipient_id=$2 AND owner_token=$3 AND state='started' RETURNING recipient_id""",
            UUID(str(batch_id)),
            recipient_id,
            UUID(str(token)),
            result.outcome,
            result.reason_code,
            result.retryable,
            result.message_id,
        )
        if row:
            await conn.execute(
                f"""UPDATE public.broadcast_batches SET started=started-1,{result.outcome}={result.outcome}+1,
                    retryable_failed=retryable_failed+$2 WHERE batch_id=$1""",
                UUID(str(batch_id)),
                int(result.outcome == "failed" and result.retryable),
            )
        return row is not None


async def finish_batch(batch_id, token):
    async with transaction() as conn:
        return (
            await conn.fetchval(
                """UPDATE public.broadcast_batches SET state='finished',finished_at=clock_timestamp(),owner_token=NULL
               WHERE batch_id=$1 AND owner_token=$2 AND pending=0 AND started=0 RETURNING batch_id""",
                UUID(str(batch_id)),
                UUID(str(token)),
            )
            is not None
        )


async def interrupt_batch(batch_id):
    """Owner is already terminal in the queue; never replay unresolved effects."""
    async with transaction() as conn:
        await conn.fetch(
            "SELECT recipient_id FROM public.broadcast_recipients WHERE batch_id=$1 ORDER BY recipient_id FOR UPDATE",
            UUID(str(batch_id)),
        )
        await conn.execute(
            """UPDATE public.broadcast_delivery_attempts SET outcome='unknown_delivery',reason_code='worker_interrupted',
               finished_at=clock_timestamp() WHERE batch_id=$1 AND outcome='started'""",
            UUID(str(batch_id)),
        )
        await conn.execute(
            """UPDATE public.broadcast_recipients SET state=CASE WHEN state='started' THEN 'unknown_delivery' ELSE 'skipped' END,
               reason_code=CASE WHEN state='started' THEN 'worker_interrupted' ELSE 'batch_interrupted' END,
               retryable=FALSE,owner_token=NULL WHERE batch_id=$1 AND state IN ('pending','started')""",
            UUID(str(batch_id)),
        )
        await conn.execute(
            """UPDATE public.broadcast_batches SET state='finished',finished_at=clock_timestamp(),owner_token=NULL,
               unknown_delivery=unknown_delivery+started,skipped=skipped+pending,started=0,pending=0
               WHERE batch_id=$1 AND state<>'finished'""",
            UUID(str(batch_id)),
        )


async def open_batches(limit=100):
    async with transaction() as conn:
        return [
            dict(row)
            for row in await conn.fetch(
                "SELECT * FROM public.broadcast_batches WHERE state<>'finished' ORDER BY created_at LIMIT $1", limit
            )
        ]


async def coverage_start():
    async with transaction() as conn:
        return await conn.fetchval("SELECT started_at FROM public.broadcast_journal_coverage WHERE singleton")


async def list_events(*, channel=None, day: date | None = None, state=None, reason=None, cursor=None, limit=50):
    async with transaction() as conn:
        rows = await conn.fetch(
            """SELECT a.attempt_id,a.batch_id,a.recipient_id,a.channel,a.operation_id,a.started_at,a.finished_at,
               a.outcome,a.reason_code,a.retryable,a.message_id,a.effective_date,a.settings_revision,b.mode
               FROM public.broadcast_delivery_attempts a JOIN public.broadcast_batches b USING(batch_id)
               WHERE ($1::text IS NULL OR a.channel=$1) AND ($2::date IS NULL OR a.effective_date=$2)
               AND ($3::text IS NULL OR a.outcome=$3) AND ($4::text IS NULL OR a.reason_code=$4)
               AND ($5::uuid IS NULL OR (a.started_at,a.attempt_id)<
                   (SELECT started_at,attempt_id FROM public.broadcast_delivery_attempts WHERE attempt_id=$5))
               ORDER BY a.started_at DESC,a.attempt_id DESC LIMIT $6""",
            channel,
            day,
            state,
            reason,
            UUID(str(cursor)) if cursor else None,
            limit + 1,
        )
        return [dict(row) for row in rows[:limit]], str(rows[limit - 1]["attempt_id"]) if len(rows) > limit else None


async def observed_counts(channel, *, day=None, mode="delivery"):
    async with transaction() as conn:
        row = await conn.fetchrow(
            """SELECT COALESCE(SUM(sent),0)::bigint AS sent,COALESCE(SUM(failed),0)::bigint AS failed,
               COALESCE(SUM(skipped),0)::bigint AS skipped,COALESCE(SUM(unknown_delivery),0)::bigint AS unknown_delivery,
               COUNT(*)::bigint AS batches FROM public.broadcast_batches WHERE channel=$1 AND mode=$2
               AND ($3::date IS NULL OR effective_date=$3)""",
            channel,
            mode,
            day,
        )
        return dict(row)


async def purge_retained_recipients():
    async with transaction() as conn:
        # At most 1000 rows per maintenance call; attempts cascade with snapshots.
        return await conn.execute("""DELETE FROM public.broadcast_recipients WHERE (batch_id,recipient_id,channel) IN
            (SELECT batch_id,recipient_id,channel FROM public.broadcast_recipients
             WHERE created_at<clock_timestamp()-INTERVAL '30 days' ORDER BY created_at LIMIT 1000)""")


async def acknowledge_horoscope(user_id, variant, recipient):
    if (
        variant not in {"today", "tomorrow"}
        or not recipient.get("delivery_claimed_at")
        or not recipient.get("subscription_fingerprint")
    ):
        return False
    async with transaction() as conn:
        row = await conn.fetchrow(
            f"SELECT sign,utc_offset,time_{variant},is_active FROM public.horoscope_subscriptions WHERE user_id=$1 FOR UPDATE",
            user_id,
        )
        if (
            row is None
            or not row["is_active"]
            or subscription_fingerprint(dict(row), variant) != recipient["subscription_fingerprint"]
        ):
            return False
        await conn.execute(
            f"""UPDATE public.horoscope_subscriptions SET last_{variant}_sent=
            CASE WHEN last_{variant}_sent IS NULL OR last_{variant}_sent<$2 THEN $2 ELSE last_{variant}_sent END
            WHERE user_id=$1""",
            user_id,
            recipient["delivery_claimed_at"],
        )
        return True
