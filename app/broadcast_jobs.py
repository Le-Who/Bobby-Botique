"""Durable broadcast snapshots and bounded delivery through the queue's owner."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from app.broadcast_delivery import DeliveryOwner, DeliveryResult
from app.queue_contracts import (
    TERMINAL_STATES,
    AdmissionState,
    JobCancelRequested,
    JobOutcome,
    JobOwnershipLost,
    JobResult,
    QueueAdmission,
)
from app.repos import broadcast_jobs as repo


class BroadcastConflict(ValueError):
    """A bounded machine-readable validation or state conflict."""

    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BroadcastConflict("invalid_operation_id") from exc


def _ids(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise BroadcastConflict("invalid_recipients")
    if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in values):
        raise BroadcastConflict("invalid_recipients")
    return tuple(sorted(set(values)))


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _batch(row, coverage=None):
    fields = (
        "batch_id",
        "parent_batch_id",
        "operation_id",
        "task_id",
        "channel",
        "mode",
        "effective_date",
        "variant",
        "created_at",
        "finished_at",
        "state",
        "total",
        "pending",
        "started",
        "sent",
        "failed",
        "skipped",
        "unknown_delivery",
        "retryable_failed",
        "settings_revision",
    )
    result = {key: _iso(row.get(key)) for key in fields}
    for key in ("batch_id", "parent_batch_id", "operation_id"):
        if result[key] is not None:
            result[key] = str(result[key])
    result["coverage_started_at"] = _iso(coverage)
    result["admission_state"] = row.get("admission_state", "unknown")
    result["admission_reason"] = row.get("admission_reason", "admission_not_confirmed")
    return result


class BroadcastService:
    def __init__(self, *, journal=repo, queue=None, bot_provider=None):
        self.journal = journal
        self._queue = queue
        self._bot_provider = bot_provider

    @property
    def queue(self):
        if self._queue is None:
            from app.queue import task_queue

            return task_queue
        return self._queue

    async def preview_offers(self, *, channel, user_ids, force=False):
        if channel not in repo.CHANNELS or type(force) is not bool:
            raise BroadcastConflict("invalid_channel")
        users = _ids(user_ids)
        rows = await self.journal.candidates(channel, users, mode="offer", force=force)
        excluded = Counter(row["reason_code"] for row in rows if row["reason_code"])
        eligible = [row["recipient_id"] for row in rows if row["reason_code"] is None]
        return {
            "channel": channel,
            "requested": len(users),
            "eligible": len(eligible),
            "excluded": dict(excluded),
            "recipient_ids": eligible,
        }

    async def enqueue_offers(self, *, channel, user_ids, operation_id, force=False):
        return await self._enqueue(
            channel=channel, user_ids=user_ids, operation_id=operation_id, mode="offer", force=force
        )

    async def _enqueue(
        self,
        *,
        channel,
        user_ids,
        operation_id,
        mode,
        force=False,
        effective_date=None,
        variant="",
        parent_batch_id=None,
        original_epochs=None,
        scheduled=False,
        recipient_snapshot=None,
        original_subscriptions=None,
    ):
        operation_id = _uuid(operation_id)
        # The persistent identity wins over a later changed body or settings.
        batch = await self.journal.read_by_operation(operation_id)
        if batch is not None:
            return {
                "batch": _batch(batch, await self.journal.coverage_start()),
                "admission": {
                    "state": batch.get("admission_state", "unknown"),
                    "task_id": batch.get("task_id"),
                    "batch_id": str(batch["batch_id"]),
                    "reason_code": batch.get("admission_reason", "admission_not_confirmed"),
                    "storage_mode": batch.get("storage_mode", "unavailable"),
                },
            }
        if batch is None:
            if channel not in repo.CHANNELS or mode not in {"offer", "delivery"} or type(force) is not bool:
                raise BroadcastConflict("invalid_channel")
            users = _ids(user_ids)
            if channel == "horoscope" and mode == "delivery" and variant not in {"today", "tomorrow"}:
                raise BroadcastConflict("invalid_variant")
            rows = (
                recipient_snapshot
                if recipient_snapshot is not None
                else await self.journal.candidates(channel, users, mode=mode, force=force, variant=variant)
            )
            if original_epochs is not None:
                for row in rows:
                    if row["privacy_epoch"] != original_epochs.get(row["recipient_id"]):
                        row["reason_code"] = "privacy_revoked"
            if original_subscriptions is not None:
                for row in rows:
                    original = original_subscriptions.get(row["recipient_id"], {})
                    if row.get("subscription_fingerprint") != original.get("subscription_fingerprint") or row.get(
                        "slot_date"
                    ) != original.get("slot_date"):
                        row["reason_code"] = "subscription_changed"
            batch_id = str(uuid5(NAMESPACE_URL, "gemaibotv2:broadcast:" + operation_id))
            if channel == "daily_challenge" and not variant:
                from app.repos.daily_2048 import get_active_daily_game_mode

                variant = await get_active_daily_game_mode()
            batch = await self.journal.create_batch(
                batch_id=batch_id,
                operation_id=operation_id,
                channel=channel,
                mode=mode,
                effective_date=effective_date or datetime.now(ZoneInfo("Europe/Kyiv")).date(),
                variant=variant,
                force=force,
                recipients=rows,
                parent_batch_id=parent_batch_id,
                scheduled=scheduled,
            )
            if batch is None:
                raise BroadcastConflict("broadcast_capacity")
            if not batch.pop("_created", False):
                return {
                    "batch": _batch(batch, await self.journal.coverage_start()),
                    "admission": {
                        "state": batch.get("admission_state", "unknown"),
                        "task_id": batch.get("task_id"),
                        "batch_id": str(batch["batch_id"]),
                        "reason_code": batch.get("admission_reason", "admission_not_confirmed"),
                        "storage_mode": batch.get("storage_mode", "unavailable"),
                    },
                }
        batch_id = str(batch["batch_id"])
        coverage = await self.journal.coverage_start()
        admission = await self.queue.enqueue_job(
            0,
            "broadcast_delivery",
            {"batch_id": batch_id},
            idempotency_key="broadcast:" + batch_id,
            lane="broadcast",
            summary={"total": batch["total"], "date": str(batch["effective_date"]), "mode": batch["mode"]},
        )
        try:
            await self.journal.record_admission(batch_id, admission)
            batch = await self.journal.read_batch(batch_id) or batch
        except Exception:
            admission = QueueAdmission(
                AdmissionState.UNKNOWN, admission.task_id, "journal_admission_unconfirmed", admission.storage_mode
            )
        batch["task_id"] = admission.task_id
        batch["admission_state"] = str(admission.state)
        batch["admission_reason"] = admission.reason_code
        return {
            "batch": _batch(batch, coverage),
            "admission": {
                "state": str(admission.state),
                "task_id": admission.task_id,
                "batch_id": batch_id,
                "reason_code": admission.reason_code,
                "storage_mode": admission.storage_mode,
            },
        }

    async def read_batch(self, batch_id):
        row = await self.journal.read_batch(_uuid(batch_id))
        if row is None:
            return None
        result = _batch(row, await self.journal.coverage_start())
        # Reading never enqueues, claims, finishes or reconciles a mutation.
        snapshot = (
            await self.queue.read_job(row["task_id"])
            if row.get("task_id")
            else await self.queue.read_job_by_operation(0, "broadcast:" + str(row["batch_id"]))
        )
        result["job"] = snapshot.as_dict() if snapshot else None
        return result

    async def read_batch_by_operation(self, operation_id):
        row = await self.journal.read_by_operation(_uuid(operation_id))
        return await self.read_batch(str(row["batch_id"])) if row else None

    async def retry_failed(self, batch_id, *, operation_id):
        operation_id = _uuid(operation_id)
        existing = await self.journal.read_by_operation(operation_id)
        parent = await self.journal.read_batch(_uuid(batch_id))
        if parent is None:
            raise BroadcastConflict("batch_missing")
        if existing is None and parent["state"] != "finished":
            raise BroadcastConflict("batch_not_terminal")
        rows = await self.journal.recipients(str(parent["batch_id"]), retryable_only=True)
        if not rows and existing is None:
            raise BroadcastConflict("no_retryable_failures")
        return await self._enqueue(
            channel=parent["channel"],
            user_ids=[row["recipient_id"] for row in rows],
            operation_id=operation_id,
            mode=parent["mode"],
            force=parent["force_offer"] if "force_offer" in parent else parent.get("force", False),
            effective_date=parent["effective_date"],
            variant=parent["variant"],
            parent_batch_id=str(parent["batch_id"]),
            original_epochs={row["recipient_id"]: row["privacy_epoch"] for row in rows},
            original_subscriptions={row["recipient_id"]: row for row in rows}
            if parent["channel"] == "horoscope" and parent["mode"] == "delivery"
            else None,
            scheduled=parent.get("scheduled", False),
        )

    async def list_delivery_events(self, *, channel=None, date=None, state=None, reason=None, cursor=None, limit=50):
        if channel is not None and channel not in repo.CHANNELS:
            raise BroadcastConflict("invalid_channel")
        if state is not None and state not in repo.OUTCOMES | {"started"}:
            raise BroadcastConflict("invalid_state")
        if reason is not None and (not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None):
            raise BroadcastConflict("invalid_reason")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BroadcastConflict("invalid_limit")
        try:
            day = datetime.fromisoformat(date).date() if isinstance(date, str) else date
        except ValueError as exc:
            raise BroadcastConflict("invalid_date") from exc
        rows, next_cursor = await self.journal.list_events(
            channel=channel, day=day, state=state, reason=reason, cursor=_uuid(cursor) if cursor else None, limit=limit
        )
        events = [
            {key: str(value) if isinstance(value, UUID) else _iso(value) for key, value in row.items()} for row in rows
        ]
        return {
            "events": events,
            "next_cursor": next_cursor,
            "coverage_started_at": _iso(await self.journal.coverage_start()),
            "retention_days": 30,
        }

    async def observed_channel_counts(self, channel, *, date=None):
        if channel not in repo.CHANNELS:
            raise BroadcastConflict("invalid_channel")
        day = datetime.fromisoformat(date).date() if isinstance(date, str) else date
        return {
            **await self.journal.observed_counts(channel, day=day, mode="delivery"),
            "mode": "delivery",
            "coverage_started_at": _iso(await self.journal.coverage_start()),
        }

    async def recover(self, limit=100):
        recovered = 0
        for batch in await self.journal.open_batches(min(max(limit, 1), 100)):
            snapshot = (
                await self.queue.read_job(batch["task_id"])
                if batch.get("task_id")
                else await self.queue.read_job_by_operation(0, "broadcast:" + str(batch["batch_id"]))
            )
            if snapshot and batch.get("admission_state") == "unknown":
                await self.journal.record_admission(
                    str(batch["batch_id"]),
                    QueueAdmission(
                        AdmissionState.EXISTING, snapshot.task_id, "admission_reconciled", snapshot.storage_mode
                    ),
                )
            if (
                snapshot
                and snapshot.state in TERMINAL_STATES
                or snapshot is None
                and (datetime.now(UTC) - batch["created_at"]).total_seconds() > 60
            ):
                await self.journal.interrupt_batch(str(batch["batch_id"]))
                recovered += 1
        await self.journal.purge_retained_recipients()
        return {"recovered": recovered}

    async def execute(self, batch_id):
        from app.queue import current_job_guard
        from app.repos.memory_consent import private_data_lease

        batch_id = _uuid(batch_id)
        guard = current_job_guard()
        if guard is None:
            raise BroadcastConflict("broadcast_owner_required")
        await guard.checkpoint()
        token = str(uuid4())
        from app.runtime_settings.lifecycle import operation_snapshot

        settings_snapshot = await operation_snapshot()
        batch = await self.journal.claim_batch(
            batch_id, guard.snapshot.task_id, token, settings_revision=settings_snapshot.revision
        )
        if batch is None:
            # A late duplicate queue reply cannot claim an old recipient again.
            return JobResult(JobOutcome.SUCCEEDED, "batch_already_owned")
        slots = asyncio.Semaphore(5)
        progress_lock = asyncio.Lock()
        bot = self._bot_provider() if self._bot_provider else None
        if bot is None:
            from app.bot_instance import get_bot

            bot = get_bot()

        async def one(recipient):
            async with slots:
                recipient_id = recipient["recipient_id"]
                recipient_token = str(uuid4())
                await guard.checkpoint()
                if not await self.journal.claim_recipient(batch_id, recipient_id, token, recipient_token):
                    return

                async def checkpoint():
                    await guard.checkpoint()
                    rows = await self.journal.candidates(
                        batch["channel"],
                        (recipient_id,),
                        mode=batch["mode"],
                        force=batch["force_offer"],
                        variant=batch["variant"],
                    )
                    live = rows[0]
                    if live["reason_code"] or live["privacy_epoch"] != recipient["privacy_epoch"]:
                        raise JobCancelRequested(live["reason_code"] or "privacy_revoked")
                    if (
                        batch["channel"] == "horoscope"
                        and batch["mode"] == "delivery"
                        and (
                            not recipient.get("subscription_fingerprint")
                            or live.get("subscription_fingerprint") != recipient.get("subscription_fingerprint")
                        )
                    ):
                        raise JobCancelRequested("subscription_changed")
                    if (
                        batch["channel"] == "horoscope"
                        and batch["mode"] == "delivery"
                        and live.get("slot_date") != recipient.get("slot_date")
                    ):
                        raise JobCancelRequested("delivery_date_expired")
                    if batch["mode"] == "delivery":
                        from app.repos.settings_repo import get_global_setting

                        setting = {
                            "daily_challenge": "daily_crocodile_delivery_enabled",
                            "horoscope": "horoscope_delivery_enabled",
                            "tarot": "tarot_daily_delivery_enabled",
                        }[batch["channel"]]
                        if str(await get_global_setting(setting, "on")).strip().lower() == "off":
                            raise JobCancelRequested("channel_paused")

                result = DeliveryResult("skipped", "privacy_revoked")
                async with private_data_lease(
                    recipient_id, recipient["privacy_epoch"], purpose="broadcast:delivery", require_ltm=False
                ) as permitted:
                    if permitted:
                        owner = DeliveryOwner(
                            self.journal,
                            batch_id=batch_id,
                            recipient_id=recipient_id,
                            token=recipient_token,
                            bot=bot,
                            checkpoint=checkpoint,
                        )
                        result = await owner.deliver(lambda proxy: self._send(batch, recipient_id, proxy))
                    if not await self.journal.finish_recipient(batch_id, recipient_id, recipient_token, result):
                        raise BroadcastConflict("recipient_disposition_unconfirmed")
                    if permitted and result.outcome == "sent":
                        await self._acknowledge(batch, recipient_id, recipient)
                    elif permitted and result.reason_code == "recipient_unreachable":
                        await self._retire_unreachable(batch, recipient_id)
                async with progress_lock:
                    current = await self.journal.read_batch(batch_id)
                    await guard.update_progress(
                        {
                            "total": current["total"],
                            "completed": current["sent"]
                            + current["failed"]
                            + current["skipped"]
                            + current["unknown_delivery"],
                            "failed": current["failed"],
                            "skipped": current["skipped"],
                            "remaining": current["pending"] + current["started"],
                        }
                    )

        tasks = [asyncio.create_task(one(recipient)) for recipient in await self.journal.recipients(batch_id)]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                await self.journal.interrupt_batch(batch_id)
                return JobResult(JobOutcome.UNKNOWN_DELIVERY, "broadcast_disposition_unconfirmed")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if not await self.journal.finish_batch(batch_id, token):
            return JobResult(JobOutcome.UNKNOWN_DELIVERY, "broadcast_journal_finish_unconfirmed")
        final = await self.journal.read_batch(batch_id)
        summary = {key: final[key] for key in ("total", "sent", "failed", "skipped", "unknown_delivery")}
        # Queue summaries use existing safe fields; detailed observed outcomes
        # remain in the journal (never leak a per-recipient queue payload).
        summary = {
            "total": summary["total"],
            "completed": summary["sent"] + summary["failed"] + summary["skipped"] + summary["unknown_delivery"],
            "failed": summary["failed"],
            "skipped": summary["skipped"],
            "remaining": 0,
        }
        if final["unknown_delivery"]:
            return JobResult(JobOutcome.UNKNOWN_DELIVERY, "broadcast_delivery_unconfirmed", summary)
        if final["failed"]:
            return JobResult(JobOutcome.PERMANENT_FAILURE, "broadcast_partial_failure", summary)
        return JobResult(JobOutcome.SUCCEEDED, "broadcast_complete", summary)

    async def _acknowledge(self, batch, user_id, recipient):
        # Receipt is already durable. A failed legacy convenience marker cannot
        # turn that confirmed send into a retryable recipient.
        try:
            if batch["channel"] == "horoscope":
                from app.repos.horoscope_subscriptions import mark_horoscope_discovery_sent

                if batch["mode"] == "offer":
                    await mark_horoscope_discovery_sent(user_id)
                else:
                    await self.journal.acknowledge_horoscope(user_id, batch["variant"], recipient)
            elif batch["channel"] == "tarot" and batch["mode"] == "offer":
                from app.repos.tarot_daily_subscriptions import mark_tarot_discovery_sent

                await mark_tarot_discovery_sent(user_id)
        except Exception:
            pass  # Journal/reservation remains the authoritative no-replay record.

    async def _retire_unreachable(self, batch, user_id):
        try:
            if batch["channel"] == "daily_challenge":
                from app.repos.crocodile_daily import snooze_discovery, unsubscribe

                if batch["mode"] == "delivery":
                    await unsubscribe(user_id)
                await snooze_discovery(user_id)
            elif batch["channel"] == "horoscope" and batch["mode"] == "delivery":
                from app.repos.horoscope_subscriptions import upsert_horoscope_subscription

                await upsert_horoscope_subscription(user_id, is_active=False)
            elif batch["channel"] == "tarot" and batch["mode"] == "delivery":
                from app.repos.tarot_daily_subscriptions import unsubscribe_tarot

                await unsubscribe_tarot(user_id)
        except Exception:
            pass

    async def enqueue_scheduled(self, *, channel, user_ids, effective_date, variant="", mode="delivery"):
        if not user_ids:
            return []
        # Reconciliation never enqueues a crashed/unknown admission. Only queue
        # proof can confirm it; absent proof is quarantined, never replayed.
        await self.recover()
        users = sorted(set(user_ids))
        results = []
        for offset in range(0, len(users), 100):
            chunk = users[offset : offset + 100]
            rows = await self.journal.candidates(channel, tuple(chunk), mode=mode, force=False, variant=variant)
            digest = hashlib.sha256(
                ",".join(
                    f"{row['recipient_id']}:{row['privacy_epoch']}:{row.get('slot_date') or effective_date}"
                    for row in rows
                ).encode()
            ).hexdigest()
            operation_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"gemaibotv2:broadcast:scheduled:{channel}:{mode}:{effective_date}:{variant}:{digest}",
                )
            )
            results.append(
                await self._enqueue(
                    channel=channel,
                    user_ids=chunk,
                    operation_id=operation_id,
                    mode=mode,
                    effective_date=effective_date,
                    variant=variant,
                    scheduled=True,
                    recipient_snapshot=rows,
                )
            )
        return results

    async def _send(self, batch, user_id, bot):
        day = batch["effective_date"]
        if isinstance(day, str):
            day = date.fromisoformat(day)
        if batch["channel"] == "daily_challenge":
            entry: Callable[..., Awaitable[Any]]
            offer: Callable[..., Awaitable[Any]]
            if batch["variant"] == "2048":
                from app.handlers.daily_2048 import send_daily2048_entry as entry
                from app.handlers.daily_2048 import send_discovery_intro as offer
            elif batch["variant"] == "trivia":
                from app.handlers.daily_trivia import send_daily_trivia_entry as entry
                from app.handlers.daily_trivia import send_discovery_intro as offer
            else:
                from app.handlers.daily_crocodile import send_daily_prompt as entry
                from app.handlers.daily_crocodile import send_discovery_intro as offer
            return await offer(bot, user_id) if batch["mode"] == "offer" else await entry(bot, user_id, day)
        if batch["channel"] == "horoscope":
            if batch["mode"] == "offer":
                from app.handlers.horoscope_subscription import send_horoscope_invite

                return await send_horoscope_invite(bot, user_id)
            from app.handlers.scheduled_horoscopes import _deliver_horoscope
            from app.repos.horoscope_subscriptions import get_horoscope_subscription

            sub = await get_horoscope_subscription(user_id)
            if sub is None:
                raise JobCancelRequested("unsubscribed")
            return await _deliver_horoscope(bot, user_id, sub["sign"], batch["variant"])
        if batch["mode"] != "offer":
            raise BroadcastConflict("tarot_delivery_owner_unavailable")
        from app.handlers.tarot_daily import send_tarot_invite

        return await send_tarot_invite(bot, user_id)


service = BroadcastService()


async def preview_offers(**kwargs):
    return await service.preview_offers(**kwargs)


async def enqueue_offers(**kwargs):
    return await service.enqueue_offers(**kwargs)


async def read_batch(batch_id):
    return await service.read_batch(batch_id)


async def read_batch_by_operation(operation_id):
    return await service.read_batch_by_operation(operation_id)


async def retry_failed(batch_id, **kwargs):
    return await service.retry_failed(batch_id, **kwargs)


async def list_delivery_events(**kwargs):
    return await service.list_delivery_events(**kwargs)


async def observed_channel_counts(channel, **kwargs):
    return await service.observed_channel_counts(channel, **kwargs)


async def recover_broadcast_jobs(limit=100):
    return await service.recover(limit)


async def execute_broadcast_batch(batch_id):
    return await service.execute(batch_id)


async def enqueue_scheduled(**kwargs):
    return await service.enqueue_scheduled(**kwargs)
