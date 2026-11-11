"""Fenced claims and receipt observations for reminders and brief slots.

Only preparation with no possible Telegram effect may be reclaimed. Every
transaction is short; provider and Telegram calls belong to the delivery owner.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.repos.broadcast_jobs import transaction

_CURRENT_SOURCE = """
    EXISTS (SELECT 1 FROM public.chats c JOIN public.users u USING(user_id)
            WHERE c.user_id=d.user_id AND c.memory_epoch=d.privacy_epoch
              AND NOT c.private_data_blocked AND (d.kind!='brief' OR c.ltm_enabled))
    AND ((d.kind='reminder' AND EXISTS (
        SELECT 1 FROM public.user_reminders r
        WHERE r.id=d.source_id AND r.user_id=d.user_id AND NOT r.is_delivered))
      OR (d.kind='brief' AND EXISTS (
        SELECT 1 FROM public.brief_subscriptions s
        WHERE s.id=d.source_id AND s.user_id=d.user_id AND s.is_active
          AND s.delivery_revision=d.source_revision
          AND (s.last_sent_at IS NULL OR (s.last_sent_at AT TIME ZONE 'UTC')::date<d.slot_date))))
"""

_RECLAIM_SAFE_FAILURE = """
    ON CONFLICT (kind,source_id,slot_date) DO UPDATE
    SET state='started',owner_token=EXCLUDED.owner_token,reason_code=NULL,
        retryable=FALSE,retry_at=NULL,claimed_at=clock_timestamp(),
        expires_at=clock_timestamp()+interval '120 seconds',finished_at=NULL,
        source_revision=EXCLUDED.source_revision,privacy_epoch=EXCLUDED.privacy_epoch,
        retry_count=CASE WHEN scheduled_deliveries.source_revision!=EXCLUDED.source_revision THEN 0
                        ELSE scheduled_deliveries.retry_count END
    WHERE (scheduled_deliveries.state='failed' AND scheduled_deliveries.retryable
           AND scheduled_deliveries.retry_at<=clock_timestamp() AND scheduled_deliveries.retry_count<3
           AND scheduled_deliveries.privacy_epoch=EXCLUDED.privacy_epoch)
       OR (scheduled_deliveries.kind='brief'
           AND scheduled_deliveries.source_revision!=EXCLUDED.source_revision
           AND scheduled_deliveries.state IN ('started','failed','skipped')
           AND NOT EXISTS (SELECT 1 FROM public.scheduled_delivery_attempts a
               WHERE a.delivery_id=scheduled_deliveries.delivery_id
                 AND (a.outcome IS NULL OR a.outcome IN ('sent','unknown_delivery'))))
    RETURNING *
"""


async def _claim_result(conn, row):
    if not row:
        return None
    result = dict(row)
    result["attempt_sequence"] = await conn.fetchval(
        "SELECT COALESCE(MAX(sequence),0) FROM public.scheduled_delivery_attempts WHERE delivery_id=$1",
        row["delivery_id"],
    )
    return result


async def claim_reminder(reminder_id):
    async with transaction() as conn:
        # /remind is available before a user's first AI conversation. Legacy
        # chatless rows still need a durable generation before any work starts.
        await conn.execute(
            """INSERT INTO public.chats(user_id)
               SELECT user_id FROM public.user_reminders WHERE id=$1
               ON CONFLICT (user_id) DO NOTHING""",
            reminder_id,
        )
        row = await conn.fetchrow(
            """INSERT INTO public.scheduled_deliveries
               (delivery_id,kind,source_id,source_revision,slot_date,user_id,privacy_epoch,owner_token)
               SELECT $2,'reminder',r.id,1,(r.trigger_at AT TIME ZONE 'UTC')::date,r.user_id,c.memory_epoch,$3
               FROM public.user_reminders r JOIN public.chats c USING(user_id)
               WHERE r.id=$1 AND NOT r.is_delivered AND r.trigger_at<=clock_timestamp()
                 AND NOT c.private_data_blocked
               """
            + _RECLAIM_SAFE_FAILURE,
            reminder_id,
            uuid4(),
            uuid4(),
        )
        return await _claim_result(conn, row)


async def claim_brief(user_id, sub_type="morning_brief", *, subscription=None):
    now = datetime.now(UTC)
    async with transaction() as conn:
        row = await conn.fetchrow(
            """INSERT INTO public.scheduled_deliveries
               (delivery_id,kind,source_id,source_revision,slot_date,user_id,privacy_epoch,owner_token)
               SELECT $3,'brief',s.id,s.delivery_revision,$4,s.user_id,c.memory_epoch,$5
               FROM public.brief_subscriptions s JOIN public.chats c USING(user_id)
               WHERE s.user_id=$1 AND s.subscription_type=$2 AND s.is_active
                 AND c.ltm_enabled AND NOT c.private_data_blocked
                 AND (s.last_sent_at IS NULL OR (s.last_sent_at AT TIME ZONE 'UTC')::date<$4)
                 AND ($6::bigint IS NULL OR (s.id=$6 AND s.delivery_revision=$7 AND s.preferred_hour=$8))
               """
            + _RECLAIM_SAFE_FAILURE,
            user_id,
            sub_type,
            uuid4(),
            now.date(),
            uuid4(),
            subscription["id"] if subscription is not None else None,
            subscription["delivery_revision"] if subscription is not None else None,
            now.hour if subscription is not None else None,
        )
        return await _claim_result(conn, row)


_DUE_BRIEF_RETRY = f"""
    d.kind='brief' AND d.state='failed' AND d.retryable
    AND d.retry_at<=clock_timestamp() AND d.retry_count<3
    AND d.slot_date=(clock_timestamp() AT TIME ZONE 'UTC')::date
    AND {_CURRENT_SOURCE}
    AND NOT EXISTS (SELECT 1 FROM public.scheduled_delivery_attempts a
        WHERE a.delivery_id=d.delivery_id
          AND (a.outcome IS NULL OR a.outcome IN ('sent','unknown_delivery')))
"""


async def get_due_brief_retries():
    """Return exact persisted daily slots independently of the initial hour."""
    async with transaction() as conn:
        rows = await conn.fetch(
            f"""SELECT d.delivery_id,d.owner_token,d.source_id,d.source_revision,
                       d.slot_date,d.user_id,d.privacy_epoch
                FROM public.scheduled_deliveries d WHERE {_DUE_BRIEF_RETRY}
                ORDER BY d.retry_at,d.delivery_id"""
        )
        return [dict(row) for row in rows]


async def claim_brief_retry(retry):
    """Reclaim the captured revision/epoch/slot, never an updated subscription."""
    async with transaction() as conn:
        row = await conn.fetchrow(
            f"""UPDATE public.scheduled_deliveries d
                SET state='started',owner_token=$8,reason_code=NULL,
                    retryable=FALSE,retry_at=NULL,claimed_at=clock_timestamp(),
                    expires_at=clock_timestamp()+interval '120 seconds',finished_at=NULL
                WHERE d.delivery_id=$1 AND d.owner_token=$2 AND d.source_id=$3
                  AND d.source_revision=$4 AND d.slot_date=$5 AND d.user_id=$6
                  AND d.privacy_epoch=$7 AND {_DUE_BRIEF_RETRY}
                RETURNING d.*""",
            retry["delivery_id"],
            retry["owner_token"],
            retry["source_id"],
            retry["source_revision"],
            retry["slot_date"],
            retry["user_id"],
            retry["privacy_epoch"],
            uuid4(),
        )
        return await _claim_result(conn, row)


async def is_current(claim):
    async with transaction() as conn:
        return bool(
            await conn.fetchval(
                f"""SELECT 1 FROM public.scheduled_deliveries d
                WHERE d.delivery_id=$1 AND d.owner_token=$2 AND d.state='started'
                  AND d.expires_at>clock_timestamp() AND {_CURRENT_SOURCE}""",
                claim["delivery_id"],
                claim["owner_token"],
            )
        )


async def renew(claim):
    async with transaction() as conn:
        return bool(
            await conn.fetchval(
                f"""UPDATE public.scheduled_deliveries d
                SET expires_at=LEAST(claimed_at+interval '15 minutes',clock_timestamp()+interval '120 seconds')
                WHERE d.delivery_id=$1 AND d.owner_token=$2 AND d.state='started'
                  AND d.expires_at>clock_timestamp() AND d.claimed_at+interval '15 minutes'>clock_timestamp()
                  AND {_CURRENT_SOURCE} RETURNING 1""",
                claim["delivery_id"],
                claim["owner_token"],
            )
        )


async def start_attempt(delivery_id, recipient_id, token, sequence):
    """DeliveryOwner journal protocol; recipient is already fenced by the claim."""
    async with transaction() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO public.scheduled_delivery_attempts(attempt_id,delivery_id,owner_token,sequence)
                SELECT $1,d.delivery_id,d.owner_token,$4 FROM public.scheduled_deliveries d
                WHERE d.delivery_id=$2 AND d.owner_token=$3 AND d.user_id=$5 AND d.state='started'
                  AND d.expires_at>clock_timestamp() AND {_CURRENT_SOURCE}
                FOR UPDATE OF d
                ON CONFLICT DO NOTHING RETURNING attempt_id""",
            uuid4(),
            UUID(str(delivery_id)),
            UUID(str(token)),
            sequence,
            recipient_id,
        )
        return dict(row) if row else None


async def finish_attempt(attempt_id, token, result):
    async with transaction() as conn:
        return bool(
            await conn.fetchval(
                """UPDATE public.scheduled_delivery_attempts a
               SET outcome=$3,reason_code=$4,message_id=$5,finished_at=clock_timestamp()
               FROM public.scheduled_deliveries d
               WHERE a.attempt_id=$1 AND a.owner_token=$2 AND a.outcome IS NULL
                 AND d.delivery_id=a.delivery_id AND d.owner_token=a.owner_token AND d.state='started'
                 AND d.expires_at>clock_timestamp() RETURNING 1""",
                attempt_id,
                UUID(str(token)),
                result.outcome,
                result.reason_code,
                result.message_id,
            )
        )


async def release_unstarted(claim):
    """Admission rejection/cancellation before any transport attempt is safe."""
    async with transaction() as conn:
        return bool(
            await conn.fetchval(
                """DELETE FROM public.scheduled_deliveries d WHERE delivery_id=$1 AND owner_token=$2
               AND state='started' AND NOT EXISTS (
                   SELECT 1 FROM public.scheduled_delivery_attempts a WHERE a.delivery_id=d.delivery_id)
               RETURNING 1""",
                claim["delivery_id"],
                claim["owner_token"],
            )
        )


async def finish_delivery(claim, result):
    async with transaction() as conn:
        # Privacy barriers take this lock before changing the generation.
        # Source markers and the final epoch check share that ordering.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", claim["user_id"])
        row = await conn.fetchrow(
            """UPDATE public.scheduled_deliveries d SET state=$3,reason_code=$4,message_id=$5,
                   retryable=($6 AND retry_count<2 AND NOT EXISTS (
                       SELECT 1 FROM public.scheduled_delivery_attempts a WHERE a.delivery_id=d.delivery_id
                       AND (a.outcome IS NULL OR a.outcome IN ('sent','unknown_delivery')))),
                   retry_count=retry_count+1,retry_at=clock_timestamp()+interval '60 seconds',
                   finished_at=clock_timestamp()
               WHERE delivery_id=$1 AND owner_token=$2 AND state='started'
                 AND expires_at>clock_timestamp()
                 AND ($3!='sent' OR (EXISTS (
                     SELECT 1 FROM public.scheduled_delivery_attempts a WHERE a.delivery_id=d.delivery_id
                       AND a.owner_token=$2 AND a.outcome='sent' AND a.message_id=$5)
                     AND NOT EXISTS (SELECT 1 FROM public.scheduled_delivery_attempts a
                         WHERE a.delivery_id=d.delivery_id AND a.owner_token=$2
                           AND (a.outcome IS NULL OR a.outcome='unknown_delivery')))) RETURNING *""",
            claim["delivery_id"],
            claim["owner_token"],
            result.outcome,
            result.reason_code,
            result.message_id,
            result.outcome == "failed" and result.retryable,
        )
        if not row:
            return False
        await conn.execute(
            """UPDATE public.scheduled_delivery_attempts SET outcome='unknown_delivery',
                   reason_code='worker_interrupted',finished_at=clock_timestamp()
               WHERE delivery_id=$1 AND owner_token=$2 AND outcome IS NULL""",
            claim["delivery_id"],
            claim["owner_token"],
        )
        if result.outcome != "sent":
            return True
        # The same source/version and account generation must still authorize
        # acknowledgment. A confirmed external send never becomes retryable.
        current = await conn.fetchval(
            f"SELECT 1 FROM public.scheduled_deliveries d WHERE delivery_id=$1 AND {_CURRENT_SOURCE}",
            claim["delivery_id"],
        )
        if not current:
            return False
        if claim["kind"] == "reminder":
            return bool(
                await conn.fetchval(
                    """UPDATE public.user_reminders SET is_delivered=TRUE WHERE id=$1 AND user_id=$2
                   AND EXISTS (SELECT 1 FROM public.chats c WHERE c.user_id=$2 AND c.memory_epoch=$3
                               AND NOT c.private_data_blocked) RETURNING 1""",
                    claim["source_id"],
                    claim["user_id"],
                    claim["privacy_epoch"],
                )
            )
        return bool(
            await conn.fetchval(
                """UPDATE public.brief_subscriptions SET last_sent_at=clock_timestamp()
               WHERE id=$1 AND user_id=$2 AND delivery_revision=$3 AND is_active
                 AND EXISTS (SELECT 1 FROM public.chats c WHERE c.user_id=$2 AND c.memory_epoch=$4
                             AND NOT c.private_data_blocked AND c.ltm_enabled) RETURNING 1""",
                claim["source_id"],
                claim["user_id"],
                claim["source_revision"],
                claim["privacy_epoch"],
            )
        )


async def recover_abandoned():
    """An expired owner cannot acknowledge late receipts or resume sending."""
    async with transaction() as conn:
        # Lock claims first, as settlement does. Never race an in-flight receipt
        # into a fresh claim; any possibly visible effect is quarantined.
        await conn.fetch(
            "SELECT delivery_id FROM public.scheduled_deliveries WHERE state='started' "
            "AND expires_at<=clock_timestamp() FOR UPDATE"
        )
        await conn.execute(
            """UPDATE public.scheduled_deliveries d SET state='failed',reason_code='worker_interrupted_no_effect',
                   retryable=(retry_count<2),retry_count=retry_count+1,
                   retry_at=clock_timestamp()+interval '60 seconds',finished_at=clock_timestamp()
               WHERE state='started' AND expires_at<=clock_timestamp()
                 AND EXISTS (SELECT 1 FROM public.scheduled_delivery_attempts a WHERE a.delivery_id=d.delivery_id)
                 AND NOT EXISTS (SELECT 1 FROM public.scheduled_delivery_attempts a
                     WHERE a.delivery_id=d.delivery_id
                       AND (a.outcome IS NULL OR a.outcome IN ('sent','unknown_delivery')))"""
        )
        await conn.execute(
            """UPDATE public.scheduled_delivery_attempts a SET outcome='unknown_delivery',
                   reason_code='worker_interrupted',finished_at=clock_timestamp()
               FROM public.scheduled_deliveries d WHERE a.delivery_id=d.delivery_id
                 AND a.outcome IS NULL AND d.state='started' AND d.expires_at<=clock_timestamp()"""
        )
        await conn.execute(
            """DELETE FROM public.scheduled_deliveries d WHERE state='started' AND expires_at<=clock_timestamp()
               AND NOT EXISTS (SELECT 1 FROM public.scheduled_delivery_attempts a
                               WHERE a.delivery_id=d.delivery_id AND a.outcome IN ('sent','unknown_delivery'))"""
        )
        await conn.execute(
            """UPDATE public.scheduled_deliveries SET state='unknown_delivery',reason_code='worker_interrupted',
                   finished_at=clock_timestamp() WHERE state='started' AND expires_at<=clock_timestamp()"""
        )
