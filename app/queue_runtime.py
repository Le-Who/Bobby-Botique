"""Queue workers and public commands over the single version-two store interface."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import math
import random
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from app import database as db
from app.observability.context import current_context, export_job_context, restore_job_context
from app.observability.events import emit, record_exception
from app.queue_contracts import (
    TERMINAL_STATES,
    AdmissionState,
    JobCancelRequested,
    JobOutcome,
    JobOwnershipLost,
    JobPage,
    JobResult,
    JobSnapshot,
    JobState,
    LaneSnapshot,
    QueueAdmission,
    QueueAdmissionUnconfirmed,
    QueueConflict,
    QueuePolicy,
    QueueUnavailable,
    normalize_job_result,
    public_summary,
)
from app.queue_legacy import Task, TaskPriority, TaskStatus
from app.queue_scheduling import next_attempt_at
from app.queue_store import LANE_POLICIES, LANE_WORKERS, JobClaim, JobEnvelope, MemoryQueueStore, RedisQueueStore

OWNER_HEARTBEAT_SECONDS = 2.0
_guard_context: contextvars.ContextVar[JobExecutionGuard | None] = contextvars.ContextVar(
    "queue_execution_guard", default=None
)


def current_job_guard() -> JobExecutionGuard | None:
    """Return the owner guard for checkpoints before each stage/external effect."""
    return _guard_context.get()


def _encode(value):
    if isinstance(value, (bytes, bytearray)):
        return {"$queue_bytes": base64.b64encode(value).decode("ascii")}
    raise TypeError("Unsupported queued payload type")


def _decode(value):
    if set(value) == {"$queue_bytes"}:
        return base64.b64decode(value["$queue_bytes"], validate=True)
    return value


@dataclass(frozen=True)
class HandlerRegistration:
    handler: Any
    lane: str
    effectful: bool = True
    image_work: bool = False
    execution_timeout_seconds: int | None = None


class JobExecutionGuard:
    def __init__(self, queue: TaskQueue, claim: JobClaim):
        self.queue = queue
        self.claim = claim
        self.snapshot = claim.snapshot
        self.lock = asyncio.Lock()

    async def checkpoint(self) -> None:
        """Raise a typed cancellation/ownership error before another side effect."""
        async with self.lock:
            store = self.queue._choose_store()
            m = await store.private_record(self.claim.snapshot.task_id)
            if not m or m["owner_token"] != self.claim.owner_token or m["state"] not in {"running", "cancel_requested"}:
                raise JobOwnershipLost("job_ownership_lost")
            if m["state"] == "cancel_requested":
                raise JobCancelRequested("cancel_requested")
            if self.claim.user_id > 0:
                from app.repos.memory_consent import is_private_data_snapshot_current

                if not await is_private_data_snapshot_current(
                    self.claim.user_id, self.claim.privacy_epoch, require_ltm=False
                ):
                    raise JobCancelRequested("privacy_revoked")
            self.snapshot = await store.heartbeat(m["task_id"], self.claim.owner_token, m["revision"])

    async def update_progress(self, summary: dict[str, int | str | bool]) -> JobSnapshot:
        clean = public_summary(summary)
        await self.checkpoint()
        async with self.lock:
            store = self.queue._choose_store()
            current = await store.private_record(self.claim.snapshot.task_id)
            if not current or current["owner_token"] != self.claim.owner_token:
                raise JobOwnershipLost("job_ownership_lost")
            if current["state"] == "cancel_requested":
                raise JobCancelRequested("cancel_requested")
            self.snapshot = await store.update_progress(
                current["task_id"], self.claim.owner_token, current["revision"], clean
            )
            self.queue._cache(self.snapshot, self.claim.user_id)
            return self.snapshot


class TaskQueue:
    """Typed admission, durable shared lifecycle, and fixed workload worker pools."""

    def __init__(self, max_workers: int = 3, *, store=None, clock=time.time, random_source=random.random):
        if type(max_workers) is not int or not 1 <= max_workers <= 3:
            raise ValueError("Deferred worker count must be between one and three")
        self.max_workers = max_workers
        self._store = store
        self._clock = clock
        self._random = random_source
        self.tasks: OrderedDict[str, Task] = OrderedDict()
        self.workers: list[asyncio.Task] = []
        self._executions: dict[str, asyncio.Task] = {}
        self._claims: dict[str, JobClaim] = {}
        self._scheduler_task: asyncio.Task | None = None
        self._metrics_scheduler_task: asyncio.Task | None = None
        self._work_available = asyncio.Event()
        self.running = False
        self._registrations: dict[str, HandlerRegistration] = {}
        self._task_handlers: dict[str, Any] = {}
        self.register_handler("deferred_ai_response", self._handle_deferred_ai_response, lane="deferred")
        self.register_handler("document_processing", self._handle_document_processing, lane="deferred")
        self.register_handler("cleanup_metrics", self._handle_cleanup_metrics, lane="maintenance", effectful=False)
        self.register_handler("broadcast_delivery", self._handle_broadcast_delivery, lane="broadcast")

    def _choose_store(self):
        if self._store is None:
            from app.queue import _get_redis

            redis = _get_redis()
            self._store = RedisQueueStore(redis) if redis is not None else MemoryQueueStore(clock=self._clock)
        return self._store

    def _read_store(self):
        if self._store is not None:
            return self._store
        from app.queue import _get_redis

        redis = _get_redis()
        if redis is None:
            raise QueueUnavailable("queue_starting")
        self._store = RedisQueueStore(redis)
        return self._store

    def register_handler(
        self,
        kind: str,
        handler,
        *,
        lane: str,
        effectful: bool = True,
        image_work: bool = False,
        execution_timeout_seconds: int | None = None,
    ):
        if self.running:
            raise QueueConflict("registration_closed")
        if lane not in LANE_WORKERS or not kind or len(kind) > 80 or not callable(handler):
            raise QueueConflict("invalid_handler_registration")
        if execution_timeout_seconds is None and lane == "daily":
            execution_timeout_seconds = 1800 if kind.startswith("tarot.") else 600
        if execution_timeout_seconds is not None and (
            type(execution_timeout_seconds) is not int or not 1 <= execution_timeout_seconds <= 1800
        ):
            raise QueueConflict("invalid_execution_budget")
        previous = self._registrations.get(kind)
        if previous and (
            previous.lane != lane
            or previous.effectful != effectful
            or previous.image_work != image_work
            or previous.execution_timeout_seconds != execution_timeout_seconds
        ):
            raise QueueConflict("handler_registration_conflict")
        self._registrations[kind] = HandlerRegistration(handler, lane, effectful, image_work, execution_timeout_seconds)
        self._task_handlers[kind] = handler

    @asynccontextmanager
    async def _privacy_scope(self, user_id, epoch, *, purpose):
        if user_id <= 0:
            yield True
            return
        from app.repos.memory_consent import private_data_lease

        async with private_data_lease(user_id, epoch, purpose=purpose, require_ltm=False) as permitted:
            yield permitted

    async def enqueue_job(
        self,
        user_id: int,
        kind: str,
        data: dict[str, Any],
        *,
        idempotency_key: str,
        lane: str,
        policy: QueuePolicy | None = None,
        priority: int | TaskPriority = 2,
        not_before: float | None = None,
        delay_seconds: float = 0,
        expected_epoch: int | None = None,
        conflict_keys: tuple[str, ...] = (),
        summary: dict[str, int | str | bool] | None = None,
    ) -> QueueAdmission:
        try:
            store = self._read_store()
        except QueueUnavailable:
            return QueueAdmission(AdmissionState.REJECTED, None, "queue_starting", "unavailable")
        registration = self._registrations.get(kind)
        if not registration or registration.lane != lane:
            return QueueAdmission(AdmissionState.REJECTED, None, "unknown_kind_or_lane", store.storage_mode)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            return QueueAdmission(AdmissionState.REJECTED, None, "invalid_operation_id", store.storage_mode)
        priority = priority.value if isinstance(priority, TaskPriority) else priority
        if type(priority) is not int or priority not in {1, 2, 3, 4}:
            return QueueAdmission(AdmissionState.REJECTED, None, "invalid_priority", store.storage_mode)
        if (
            not math.isfinite(delay_seconds)
            or delay_seconds < 0
            or (not_before is not None and not math.isfinite(not_before))
        ):
            return QueueAdmission(AdmissionState.REJECTED, None, "invalid_schedule", store.storage_mode)
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"queue:v2:{user_id}:{idempotency_key}"))
        try:
            payload = json.dumps(
                {"data": data, "context": export_job_context()},
                ensure_ascii=False,
                separators=(",", ":"),
                default=_encode,
            )
        except TypeError, ValueError:
            return QueueAdmission(AdmissionState.REJECTED, task_id, "invalid_job_payload", store.storage_mode)
        epoch = None
        if user_id > 0:
            from app.repos.memory_consent import resolve_current_epoch

            if expected_epoch is None:
                return QueueAdmission(AdmissionState.REJECTED, task_id, "privacy_epoch_missing", store.storage_mode)
            current_epoch = await resolve_current_epoch(user_id, require_ltm=False)
            epoch = expected_epoch
            if current_epoch is None or current_epoch != expected_epoch:
                return QueueAdmission(AdmissionState.REJECTED, task_id, "privacy_unavailable", store.storage_mode)
        delay = max(delay_seconds, max(0, not_before - self._clock()) if not_before is not None else 0)
        envelope = JobEnvelope(
            task_id,
            kind,
            lane,
            user_id,
            payload,
            idempotency_key,
            privacy_epoch=epoch,
            priority=priority,
            delay_seconds=delay,
            effectful=registration.effectful,
            image_work=registration.image_work,
            conflict_keys=conflict_keys,
            public_summary=public_summary(summary or {}),
            execution_timeout_seconds=registration.execution_timeout_seconds,
        )
        async with self._privacy_scope(user_id, epoch, purpose="queue:admission") as permitted:
            if not permitted:
                return QueueAdmission(AdmissionState.REJECTED, task_id, "privacy_revoked", store.storage_mode)
            admission = await store.admit(envelope, policy or LANE_POLICIES[lane])
        emit(
            "job.enqueued"
            if admission.state is AdmissionState.ACCEPTED
            else "job.capacity_rejected"
            if admission.state is AdmissionState.REJECTED
            else "job.admission_reconciled",
            operation="job.enqueue",
            job_id=admission.task_id,
            task_type=kind,
            lane=lane,
            backend=store.storage_mode,
            admission_state=admission.state.value,
            reason_code=admission.reason_code,
            priority=priority,
        )
        if admission.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING}:
            self._work_available.set()
            try:
                snapshot = await store.read_job(admission.task_id)
                if snapshot:
                    self._cache(snapshot, user_id)
            except Exception:
                # Admission is already confirmed; an optional cache refresh is
                # not authority to turn it into a rejection or another enqueue.
                pass
        return admission

    async def add_task(
        self, user_id: int, task_type: str, data: dict[str, Any], priority: TaskPriority = TaskPriority.NORMAL
    ) -> str:
        operation = current_context().request_id
        if task_type == "cleanup_metrics":
            operation = f"metrics:{int(self._clock() // 86400)}"
        if not operation:
            # The legacy interface has no operation identity. Its one-shot callers
            # get one identity; request-aware callers use enqueue_job explicitly.
            operation = uuid.uuid4().hex
        registration = self._registrations.get(task_type)
        if not registration:
            return ""
        result = await self.enqueue_job(
            user_id,
            task_type,
            data,
            idempotency_key=f"{task_type}:{operation}",
            lane=registration.lane,
            priority=priority,
        )
        if result.state is AdmissionState.UNKNOWN:
            raise QueueAdmissionUnconfirmed(result.task_id)
        return result.task_id or "" if result.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING} else ""

    async def read_job(self, task_id: str) -> JobSnapshot | None:
        snapshot = await self._read_store().read_job(task_id)
        if snapshot:
            self._cache(snapshot)
        return snapshot

    async def read_job_by_operation(self, user_id: int, idempotency_key: str) -> JobSnapshot | None:
        snapshot = await self._read_store().read_job_by_operation(user_id, idempotency_key)
        if snapshot:
            self._cache(snapshot, user_id)
        return snapshot

    async def read_jobs_by_conflicts(
        self, user_id: int, lane: str, conflict_keys: tuple[str, ...]
    ) -> tuple[JobSnapshot, ...]:
        snapshots = await self._read_store().read_jobs_by_conflicts(user_id, lane, conflict_keys)
        for snapshot in snapshots:
            self._cache(snapshot, user_id)
        return snapshots

    async def get_task_status(self, task_id: str) -> Task | None:
        snapshot = await self.read_job(task_id)
        return self.tasks.get(task_id) if snapshot else None

    def _cache(self, snapshot: JobSnapshot, user_id: int = 0):
        cached = self.tasks.get(snapshot.task_id)
        if cached and getattr(cached, "revision", 0) > snapshot.revision:
            return
        states = {
            JobState.QUEUED: TaskStatus.PENDING,
            JobState.DELAYED: TaskStatus.PENDING,
            JobState.RUNNING: TaskStatus.RUNNING,
            JobState.CANCEL_REQUESTED: TaskStatus.RUNNING,
            JobState.SUCCEEDED: TaskStatus.COMPLETED,
            JobState.FAILED: TaskStatus.FAILED,
            JobState.EXPIRED: TaskStatus.FAILED,
            JobState.CANCELLED: TaskStatus.CANCELLED,
            JobState.UNKNOWN_DELIVERY: TaskStatus.UNKNOWN_DELIVERY,
        }
        task = Task(
            snapshot.task_id,
            user_id or (cached.user_id if cached else 0),
            snapshot.kind,
            {},
            TaskPriority.NORMAL,
            states[snapshot.state],
            datetime.fromtimestamp(snapshot.submitted_at, UTC),
            started_at=datetime.fromtimestamp(snapshot.started_at, UTC) if snapshot.started_at else None,
            completed_at=datetime.fromtimestamp(snapshot.finished_at, UTC) if snapshot.finished_at else None,
            result={"outcome": snapshot.state.value, "summary": dict(snapshot.result_summary)},
            error=snapshot.reason_code,
            retry_count=snapshot.attempts_started,
            max_retries=snapshot.max_attempts,
            attempts_started=snapshot.attempts_started,
        )
        task.revision = snapshot.revision
        self.tasks[snapshot.task_id] = task
        self.tasks.move_to_end(snapshot.task_id)
        while len(self.tasks) > 256:
            self.tasks.popitem(last=False)

    async def list_jobs(
        self, *, lane: str | None = None, state: JobState | None = None, cursor: str | None = None, limit: int = 50
    ) -> JobPage:
        return await self._read_store().list_jobs(lane=lane, state=state, cursor=cursor, limit=limit)

    async def read_lane(self, lane: str) -> LaneSnapshot:
        return await self._read_store().read_lane(lane)

    async def set_lane_state(
        self,
        lane: str,
        *,
        expected_revision: int,
        admission_paused: bool,
        claim_paused: bool,
        actor: str,
        operation_id: str,
    ) -> LaneSnapshot:
        result = await self._read_store().set_lane_state(
            lane,
            expected_revision=expected_revision,
            admission_paused=admission_paused,
            claim_paused=claim_paused,
            actor=actor,
            operation_id=operation_id,
        )
        self._work_available.set()
        return result

    async def request_cancel(
        self, task_id: str, *, expected_revision: int, actor: str, operation_id: str
    ) -> JobSnapshot:
        result = await self._read_store().request_cancel(
            task_id, expected_revision=expected_revision, actor=actor, operation_id=operation_id
        )
        self._cache(result)
        return result

    async def cancel_task(self, task_id: str, user_id: int) -> bool:
        private = await self._read_store().private_record(task_id)
        if (
            not private
            or private["user_id"] != user_id
            or private["state"] not in {"queued", "delayed", "running", "cancel_requested"}
        ):
            return False
        await self.request_cancel(
            task_id, expected_revision=private["revision"], actor=f"user:{user_id}", operation_id=f"cancel:{task_id}"
        )
        return True

    async def retry_job(self, task_id: str, *, expected_revision: int, actor: str, operation_id: str) -> QueueAdmission:
        store = self._read_store()
        private = await store.private_record(task_id)
        if not private:
            raise QueueConflict("retry_not_available")
        epoch = None
        if private["user_id"] > 0:
            from app.repos.memory_consent import resolve_current_epoch

            epoch = await resolve_current_epoch(private["user_id"], require_ltm=False)
            if epoch is None or epoch != private["privacy_epoch"]:
                raise QueueConflict("privacy_revoked")
        async with self._privacy_scope(private["user_id"], epoch, purpose="queue:retry") as permitted:
            if not permitted:
                raise QueueConflict("privacy_revoked")
            result = await store.retry_job(
                task_id,
                expected_revision=expected_revision,
                actor=actor,
                operation_id=operation_id,
                privacy_epoch=epoch,
            )
        self._work_available.set()
        return result

    async def start(self):
        if self.running:
            return
        self.running = True
        self._choose_store()
        from app.utils.background_tasks import start_background_task

        for lane, amount in LANE_WORKERS.items():
            for number in range(self.max_workers if lane == "deferred" else amount):
                self.workers.append(start_background_task(None, partial(self._worker, lane), f"queue-{lane}-{number}"))
        self._scheduler_task = start_background_task(self._scheduler_task, self._schedule, "queue scheduler")
        self._metrics_scheduler_task = start_background_task(
            self._metrics_scheduler_task, self._schedule_metrics_cleanup, "queue metrics scheduler"
        )
        self._work_available.set()

    async def stop(self):
        self.running = False
        owned = [*self.workers, *[task for task in (self._scheduler_task, self._metrics_scheduler_task) if task]]
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        self.workers.clear()
        self._scheduler_task = self._metrics_scheduler_task = None

    async def _schedule(self):
        while True:
            try:
                await self._choose_store().sweep()
                self._work_available.set()
            except Exception as error:
                logging.warning("Queue scheduler unavailable (%s)", type(error).__name__)
            await asyncio.sleep(0.5)

    async def _schedule_metrics_cleanup(self):
        while True:
            await asyncio.sleep(86400)
            await self.add_task(0, "cleanup_metrics", {}, TaskPriority.LOW)

    async def _heartbeat(self, guard: JobExecutionGuard, execution: asyncio.Task):
        while True:
            await asyncio.sleep(OWNER_HEARTBEAT_SECONDS)
            try:
                await guard.checkpoint()
            except Exception:
                execution.cancel()
                return

    async def _run_claim(self, claim: JobClaim, guard: JobExecutionGuard) -> JobResult:
        token = _guard_context.set(guard)
        try:
            registration = self._registrations.get(claim.snapshot.kind)
            if not registration or registration.lane != claim.snapshot.lane:
                return JobResult(JobOutcome.PERMANENT_FAILURE, "unknown_task_type")
            async with self._privacy_scope(claim.user_id, claim.privacy_epoch, purpose="queue:execution") as permitted:
                if not permitted:
                    return JobResult(JobOutcome.PERMANENT_FAILURE, "privacy_revoked")
                await guard.checkpoint()
                try:
                    private_payload = await self._choose_store().load_payload(claim.snapshot.task_id, claim.owner_token)
                    payload = json.loads(private_payload, object_hook=_decode)
                    data = payload["data"]
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError, KeyError, TypeError:
                    return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_job_payload")
                with restore_job_context(
                    payload.get("context") or {}, task_id=claim.snapshot.task_id, execution_id=claim.owner_token
                ):
                    task = Task(
                        claim.snapshot.task_id,
                        claim.user_id,
                        claim.snapshot.kind,
                        data,
                        TaskPriority.NORMAL,
                        TaskStatus.RUNNING,
                        datetime.fromtimestamp(claim.snapshot.submitted_at, UTC),
                    )
                    return await self._execute_task(task)
        finally:
            _guard_context.reset(token)

    async def _execute_task(self, task: Task) -> JobResult:
        from app.runtime_settings.lifecycle import runtime_settings_scope

        handler = self._task_handlers.get(task.task_type)
        if not handler:
            return JobResult(JobOutcome.PERMANENT_FAILURE, "unknown_task_type")
        async with runtime_settings_scope():
            return normalize_job_result(await handler(**task.data))

    async def _finish_claim(self, claim: JobClaim, result: JobResult):
        store = self._choose_store()
        delay = (
            next_attempt_at(claim.snapshot.attempts_started, 0, result.retry_after, self._random())
            if result.outcome is JobOutcome.RETRYABLE_FAILURE
            else 0
        )
        try:
            current = await store.private_record(claim.snapshot.task_id)
            if not current or current["owner_token"] != claim.owner_token:
                raise JobOwnershipLost("job_ownership_lost")
            snapshot = await store.finish(
                claim.snapshot.task_id, claim.owner_token, current["revision"], result, delay_seconds=delay
            )
            self._cache(snapshot, claim.user_id)
            outcome = (
                "succeeded"
                if snapshot.state is JobState.SUCCEEDED
                else "unknown"
                if snapshot.state is JobState.UNKNOWN_DELIVERY
                else "cancelled"
                if snapshot.state is JobState.CANCELLED
                else "failed"
            )
            disposition = "scheduled" if snapshot.state in {JobState.QUEUED, JobState.DELAYED} else "terminal"
            emit(
                "job.finished",
                operation="job.execute",
                job_id=snapshot.task_id,
                task_type=snapshot.kind,
                lane=snapshot.lane,
                outcome=outcome,
                business_outcome=result.outcome.value,
                reason_code=snapshot.reason_code,
                retry_disposition=disposition,
                attempts_started=snapshot.attempts_started,
                retry_count=snapshot.attempts_started,
                duration_ms=round((self._clock() - (claim.snapshot.started_at or self._clock())) * 1000, 2),
            )
            if disposition == "scheduled":
                emit(
                    "job.retry_scheduled",
                    operation="job.retry",
                    job_id=snapshot.task_id,
                    task_type=snapshot.kind,
                    lane=snapshot.lane,
                    delay_seconds=delay,
                    retry_count=snapshot.attempts_started,
                    reason_code=result.reason_code,
                )
        except QueueUnavailable, QueueConflict, JobOwnershipLost, ConnectionError, TimeoutError:
            cached = self.tasks.get(claim.snapshot.task_id)
            if cached is not None and cached.revision <= claim.snapshot.revision:
                cached.status = TaskStatus.UNKNOWN_DELIVERY if claim.effectful else TaskStatus.DISPOSITION_UNCONFIRMED
                cached.error = "disposition_unconfirmed"
                cached.completed_at = datetime.fromtimestamp(self._clock(), UTC)
            emit(
                "job.finished",
                level="warning",
                operation="job.execute",
                job_id=claim.snapshot.task_id,
                task_type=claim.snapshot.kind,
                lane=claim.snapshot.lane,
                outcome="unknown",
                retry_disposition="unconfirmed",
                reason_code="disposition_unconfirmed",
            )

    async def _worker(self, lane: str):
        while True:
            claim = None
            execution = heartbeat = None
            try:
                claim = await self._choose_store().claim(lane, uuid.uuid4().hex)
                if claim is None:
                    self._work_available.clear()
                    try:
                        await asyncio.wait_for(self._work_available.wait(), 0.5)
                    except TimeoutError:
                        pass
                    continue
                self._claims[claim.snapshot.task_id] = claim
                self._cache(claim.snapshot, claim.user_id)
                emit(
                    "job.started",
                    operation="job.execute",
                    job_id=claim.snapshot.task_id,
                    task_type=claim.snapshot.kind,
                    lane=lane,
                    attempts_started=claim.snapshot.attempts_started,
                    retry_count=claim.snapshot.attempts_started - 1,
                    queue_wait_ms=round((claim.snapshot.started_at - claim.snapshot.submitted_at) * 1000, 2),
                )
                guard = JobExecutionGuard(self, claim)
                execution = asyncio.create_task(
                    self._run_claim(claim, guard), name=f"queue-job-{claim.snapshot.task_id}"
                )
                self._executions[claim.snapshot.task_id] = execution
                heartbeat = asyncio.create_task(
                    self._heartbeat(guard, execution), name=f"queue-heartbeat-{claim.snapshot.task_id}"
                )
                try:
                    result = await execution
                except JobCancelRequested as error:
                    result = JobResult(JobOutcome.PERMANENT_FAILURE, error.reason_code)
                except asyncio.CancelledError:
                    worker = asyncio.current_task()
                    if worker is not None and worker.cancelling():
                        result = JobResult(
                            JobOutcome.UNKNOWN_DELIVERY if claim.effectful else JobOutcome.RETRYABLE_FAILURE,
                            "worker_interrupted",
                        )
                        await self._finish_claim(claim, result)
                        raise
                    result = JobResult(
                        JobOutcome.UNKNOWN_DELIVERY if claim.effectful else JobOutcome.RETRYABLE_FAILURE,
                        "execution_interrupted",
                    )
                except JobOwnershipLost:
                    continue
                except Exception as error:
                    record_exception(
                        "job.execution_failed",
                        error,
                        operation="job.execute",
                        fields={"job_id": claim.snapshot.task_id, "task_type": claim.snapshot.kind},
                    )
                    result = JobResult(
                        JobOutcome.UNKNOWN_DELIVERY if claim.effectful else JobOutcome.RETRYABLE_FAILURE,
                        "document_storage_unconfirmed"
                        if claim.snapshot.kind == "document_processing"
                        else "delivery_unconfirmed"
                        if claim.effectful
                        else "handler_failed",
                    )
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                await self._finish_claim(claim, result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.warning("Queue worker unavailable (%s)", type(error).__name__)
                await asyncio.sleep(0.5)
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                if execution is not None and not execution.done():
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                if claim:
                    self._executions.pop(claim.snapshot.task_id, None)
                    self._claims.pop(claim.snapshot.task_id, None)

    async def purge_user_jobs(self, user_id: int):
        executions = [
            execution for task_id, execution in self._executions.items() if self._claims[task_id].user_id == user_id
        ]
        for execution in executions:
            execution.cancel()
        await asyncio.gather(*executions, return_exceptions=True)
        await self._read_store().erase_user(user_id)
        from app.queue import _get_redis
        from app.queue_legacy import erase_legacy_user

        redis = _get_redis()
        if redis is not None:
            if self._choose_store().storage_mode == "local":
                await RedisQueueStore(redis).erase_user(user_id)
            await erase_legacy_user(redis, user_id)
        for task_id, task in list(self.tasks.items()):
            if task.user_id == user_id:
                self.tasks.pop(task_id, None)

    async def get_queue_stats(self) -> dict[str, Any]:
        try:
            store = self._read_store()
        except QueueUnavailable:
            return {
                "available": False,
                "observed_at": self._clock(),
                "scope": "unavailable",
                "backend": "starting",
                "reason_code": "queue_starting",
                "execution_scope": "process",
                "queue_size": None,
                "local_queue_size": None,
                "redis_queue_size": None,
                "redis_available": None,
                "active_workers": 0,
                "total_tasks": None,
                "pending_tasks": None,
                "running_tasks": None,
                "completed_tasks": None,
                "failed_tasks": None,
                "unknown_delivery_tasks": None,
                "cancel_requested_tasks": None,
                "cancelled_tasks": None,
                "delayed_tasks": None,
                "payload_bytes": None,
                "lanes": {
                    lane: {
                        "available": False,
                        "scope": "unavailable",
                        "observed_at": self._clock(),
                        "reason_code": "queue_starting",
                    }
                    for lane in LANE_WORKERS
                },
                "legacy": {
                    "available": False,
                    "ready": None,
                    "processing": None,
                    "reason_code": "legacy_source_unavailable",
                },
            }
        lanes = {}
        available = True
        for lane in LANE_WORKERS:
            try:
                stats = await store.stats(lane)
                state = await store.read_lane(lane)
                lanes[lane] = {
                    **stats,
                    "revision": state.revision,
                    "admission_paused": state.admission_paused,
                    "claim_paused": state.claim_paused,
                    "workers": state.workers,
                    "policy": state.policy.as_dict(),
                }
            except Exception:
                lanes[lane] = {
                    "available": False,
                    "scope": "shared" if store.storage_mode == "redis" else "process",
                    "observed_at": self._clock(),
                }
                available = False

        def totals(name):
            return sum(lane.get(name, 0) for lane in lanes.values()) if available else None

        legacy: dict[str, Any] = {"available": True, "ready": 0, "processing": 0, "reason_code": None}
        from app.queue import _get_redis

        redis = _get_redis()
        if redis is not None:
            from app.queue_legacy import inspect_legacy

            try:
                legacy = await inspect_legacy(redis)
            except Exception:
                legacy = {
                    "available": False,
                    "ready": None,
                    "processing": None,
                    "reason_code": "legacy_source_unavailable",
                }
        return {
            "available": available,
            "observed_at": self._clock(),
            "scope": "shared" if store.storage_mode == "redis" else "process",
            "execution_scope": "process",
            "backend": ("memory" if store.storage_mode == "local" else "redis") if available else "redis_unavailable",
            "redis_available": available if store.storage_mode == "redis" else None,
            "total_tasks": totals("nonterminal"),
            "pending_tasks": (totals("queued") + totals("delayed")) if available else None,
            "waiting_tasks": (totals("queued") + totals("delayed")) if available else None,
            "running_tasks": totals("running"),
            "completed_tasks": totals("succeeded"),
            "failed_tasks": totals("failed"),
            "unknown_delivery_tasks": totals("unknown_delivery"),
            "cancel_requested_tasks": totals("cancel_requested"),
            "cancelled_tasks": totals("cancelled"),
            "delayed_tasks": totals("delayed"),
            "retrying_tasks": totals("retrying"),
            "payload_bytes": totals("payload_bytes"),
            "unconfirmed_disposition_tasks": sum(
                task.error == "disposition_unconfirmed" for task in self.tasks.values()
            ),
            "unconfirmed_disposition_scope": "process",
            "queue_size": (totals("queued") + totals("delayed")) if available else None,
            "local_queue_size": totals("queued") if store.storage_mode == "local" else 0,
            "redis_queue_size": totals("queued") if store.storage_mode == "redis" else None,
            "active_workers": sum(not worker.done() for worker in self.workers),
            "lanes": lanes,
            "legacy": legacy,
        }

    async def _handle_broadcast_delivery(self, batch_id, **kwargs) -> JobResult:
        from app.broadcast_jobs import execute_broadcast_batch

        return await execute_broadcast_batch(batch_id)

    async def _handle_document_processing(self, **kwargs) -> JobResult:
        """Обработчик для обработки документов"""
        try:
            from app.document_processor import process_uploaded_document

            file_data = kwargs.get("file_data")
            filename = kwargs.get("filename")
            user_id = kwargs.get("user_id")

            if not all([file_data, filename, user_id]):
                return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_document_parameters")

            try:
                document_user_id = int(user_id)  # type: ignore[arg-type]
            except TypeError, ValueError, OverflowError:
                return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_document_parameters")
            result = await process_uploaded_document(file_data, str(filename), document_user_id)

            if result.get("error"):
                if result.get("write_started") is True or result.get("error_stage") == "storage_write":
                    return JobResult(JobOutcome.UNKNOWN_DELIVERY, "document_storage_unconfirmed")
                if (
                    result.get("error") == "storage_error"
                    and result.get("error_stage") == "duplicate_lookup"
                    and result.get("write_started") is False
                    and result.get("retryable") is True
                ):
                    return JobResult(JobOutcome.RETRYABLE_FAILURE, "document_storage_unavailable")
                if result.get("error") == "storage_error" and not (
                    result.get("error_stage") == "duplicate_lookup" and result.get("write_started") is False
                ):
                    return JobResult(JobOutcome.UNKNOWN_DELIVERY, "document_storage_unconfirmed")
                return JobResult(JobOutcome.PERMANENT_FAILURE, "document_processing_failed")

            return JobResult(
                JobOutcome.SUCCEEDED,
                result_summary={
                    "pages": result.get("pages", 0),
                    "text_length": result.get("text_length", 0),
                    "paragraphs": result.get("paragraphs", 0),
                    "tables": result.get("tables", 0),
                },
            )

        except Exception as e:
            logging.error("Error in document processing task: %s", e, exc_info=True)
            return JobResult(JobOutcome.UNKNOWN_DELIVERY, "document_storage_unconfirmed")

    async def _handle_cleanup_metrics(self, **kwargs) -> JobResult:
        """Обработчик для очистки старых метрик"""
        try:
            await db.db_query("""
                DELETE FROM metrics
                WHERE metric_date < CURRENT_DATE - INTERVAL '30 days'
            """)

            await db.db_query("""
                DELETE FROM error_logs
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
            """)

            return JobResult(JobOutcome.SUCCEEDED)
        except Exception as e:
            logging.error("Error cleaning up metrics: %s", e, exc_info=True)
            return JobResult(JobOutcome.RETRYABLE_FAILURE, "metrics_cleanup_failed")

    async def _handle_deferred_ai_response(self, **kwargs) -> JobResult:
        """Handler for deferred AI generation retry (Plan §5)."""
        from app.deferred_response import handle_deferred_ai_response

        return await handle_deferred_ai_response(**kwargs)
