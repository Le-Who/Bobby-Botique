"""Version-two queue storage: atomic Redis commands and explicit local fallback.

No command changes backends after attempting a Redis mutation. Public snapshots
contain metadata only; payloads and account generations stay behind this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from app.queue_contracts import (
    BROADCAST_QUEUE_POLICY,
    DAILY_QUEUE_POLICY,
    DEFAULT_QUEUE_POLICY,
    AdmissionState,
    JobOutcome,
    JobPage,
    JobResult,
    JobSnapshot,
    JobState,
    LaneSnapshot,
    QueueAdmission,
    QueueConflict,
    QueuePolicy,
    QueueUnavailable,
    public_summary,
)

QUEUE_NAMESPACE = "gemaibotv2:queue:v2"
METADATA_RETENTION = 24 * 3600
RETRY_PAYLOAD_RETENTION = 900
OWNER_LEASE_SECONDS = 60
EXECUTION_SETTLEMENT_GRACE_SECONDS = 10
LANE_WORKERS = {"deferred": 3, "maintenance": 1, "daily": 2, "broadcast": 1}
LANE_POLICIES = {
    "deferred": DEFAULT_QUEUE_POLICY,
    "maintenance": DEFAULT_QUEUE_POLICY,
    "daily": DAILY_QUEUE_POLICY,
    "broadcast": BROADCAST_QUEUE_POLICY,
}
_ACTIVE = {"queued", "delayed", "running", "cancel_requested"}


def _bounded_policy(lane: str, policy: QueuePolicy) -> QueuePolicy:
    ceiling = LANE_POLICIES[lane]
    return QueuePolicy(**{name: min(value, ceiling.as_dict()[name]) for name, value in policy.as_dict().items()})


def _normalized_conflicts(keys: tuple[str, ...]) -> tuple[str, ...]:
    if len(keys) > 8 or any(not isinstance(key, str) or not key or len(key) > 128 for key in keys):
        raise QueueConflict("invalid_conflict_keys")
    return tuple(sorted(set(keys)))


@dataclass(frozen=True, slots=True)
class JobClaim:
    snapshot: JobSnapshot
    payload: str | None
    user_id: int
    privacy_epoch: int | None
    owner_token: str
    effectful: bool
    image_work: bool


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    task_id: str
    kind: str
    lane: str
    user_id: int
    payload: str
    idempotency_key: str
    privacy_epoch: int | None = None
    priority: int = 2
    delay_seconds: float = 0
    effectful: bool = True
    image_work: bool = False
    parent_task_id: str | None = None
    retry_grants: int = 1
    conflict_keys: tuple[str, ...] = ()
    public_summary: dict[str, int | str | bool] = field(default_factory=dict)
    execution_timeout_seconds: int | None = None

    def __post_init__(self):
        if self.execution_timeout_seconds is not None and (
            type(self.execution_timeout_seconds) is not int or not 1 <= self.execution_timeout_seconds <= 1800
        ):
            raise QueueConflict("invalid_execution_budget")
        object.__setattr__(self, "conflict_keys", _normalized_conflicts(self.conflict_keys))
        object.__setattr__(self, "public_summary", public_summary(self.public_summary))


def _identity(job: JobEnvelope) -> str:
    return hashlib.sha256(f"{job.user_id}:{job.idempotency_key}".encode()).hexdigest()


def _fingerprint(job: JobEnvelope) -> str:
    body = asdict(job)
    body.pop("task_id")
    body.pop("public_summary")
    try:
        payload = json.loads(body["payload"])
        if isinstance(payload, dict) and "data" in payload and "context" in payload:
            body["payload"] = payload["data"]
    except ValueError, TypeError:
        pass
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _snapshot(record: dict[str, Any], storage_mode: str) -> JobSnapshot:
    values: dict[str, Any] = {
        name: record.get(name)
        for name in JobSnapshot.__dataclass_fields__
        if name not in {"state", "storage_mode", "retry_eligible", "retry_payload_available", "result_summary"}
    }
    return JobSnapshot(
        **values,
        state=JobState(record["state"]),
        storage_mode=storage_mode,
        retry_eligible=bool(record.get("retry_eligible")),
        retry_payload_available=bool(record.get("retry_payload_available")),
        result_summary=record.get("result_summary") or {},
    )


class MemoryQueueStore:
    """The local adapter has the same transitions, with process-only durability."""

    storage_mode = "local"

    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.jobs: dict[str, dict[str, Any]] = {}
        self.payloads: dict[str, str] = {}
        self.identities: dict[str, tuple[str, str, float]] = {}
        self.sequence = 0
        self.lanes: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, tuple[str, Any, float]] = {}
        self.conflicts: dict[tuple[str, int, str], str] = {}

    async def admit(self, job: JobEnvelope, policy: QueuePolicy) -> QueueAdmission:
        self._sweep()
        return self._admit(job, policy)

    def _admit(self, job: JobEnvelope, policy: QueuePolicy, *, payload_credit: int = 0) -> QueueAdmission:
        if job.lane not in LANE_POLICIES:
            return QueueAdmission(AdmissionState.REJECTED, None, "unknown_lane")
        policy = _bounded_policy(job.lane, policy)
        now = self.clock()
        identity, fingerprint = _identity(job), _fingerprint(job)
        previous = self.identities.get(identity)
        if previous and previous[2] > now:
            return QueueAdmission(
                AdmissionState.EXISTING if previous[1] == fingerprint else AdmissionState.REJECTED,
                previous[0],
                None if previous[1] == fingerprint else "idempotency_conflict",
            )
        for key in job.conflict_keys:
            owner = self.conflicts.get((job.lane, job.user_id, key))
            if owner and self.jobs.get(owner, {}).get("state") in _ACTIVE:
                aliases = self.jobs[owner].setdefault("identity_aliases", [self.jobs[owner]["identity"]])
                if len(aliases) >= 256:
                    return QueueAdmission(AdmissionState.REJECTED, None, "idempotency_capacity")
                aliases.append(identity)
                self.identities[identity] = (owner, fingerprint, now + METADATA_RETENTION)
                return QueueAdmission(AdmissionState.EXISTING, owner, "conflict_existing")
        reason = None
        size = len(job.payload.encode("utf-8"))
        active = [
            m
            for m in self.jobs.values()
            if m["lane"] == job.lane and m["state"] in {"queued", "delayed", "running", "cancel_requested"}
        ]
        used = sum(
            m["payload_bytes"] for m in self.jobs.values() if m["lane"] == job.lane and m["task_id"] in self.payloads
        )
        lane = self.lanes.setdefault(
            job.lane, {"revision": 0, "admission_paused": False, "claim_paused": False, "policy": policy}
        )
        policy = QueuePolicy(
            **{name: min(value, lane["policy"].as_dict()[name]) for name, value in policy.as_dict().items()}
        )
        if lane["admission_paused"]:
            reason = "admission_paused"
        elif size > policy.max_payload_bytes:
            reason = "payload_too_large"
        elif len(active) >= policy.max_jobs:
            reason = "job_capacity"
        elif used + size - payload_credit > policy.max_total_payload_bytes:
            reason = "byte_capacity"
        if reason:
            return QueueAdmission(AdmissionState.REJECTED, None, reason)
        self.sequence += 1
        record = {
            **asdict(job),
            "schema_version": 2,
            "state": "delayed" if job.delay_seconds > 0 else "queued",
            "revision": 1,
            "attempts_started": 0,
            "max_attempts": policy.max_attempts,
            "submitted_at": now,
            "started_at": None,
            "finished_at": None,
            "not_before": now + max(0, job.delay_seconds),
            "deadline_at": now + policy.max_age_seconds,
            "queue_deadline_at": now + policy.max_age_seconds,
            "heartbeat_at": None,
            "reason_code": None,
            "retry_eligible": False,
            "retry_payload_available": False,
            "result_summary": dict(job.public_summary),
            "payload_bytes": size,
            "sequence": self.sequence,
            "identity": identity,
            "identity_aliases": [identity],
            "fingerprint": fingerprint,
            "owner_token": None,
            "metadata_expires_at": None,
            "payload_expires_at": now
            + (min(900, policy.max_age_seconds) if job.user_id > 0 else policy.max_age_seconds),
        }
        record.pop("payload")
        record.pop("idempotency_key")
        record.pop("public_summary")
        self.jobs[job.task_id] = record
        self.payloads[job.task_id] = job.payload
        self.identities[identity] = (job.task_id, fingerprint, now + METADATA_RETENTION)
        for key in job.conflict_keys:
            self.conflicts[(job.lane, job.user_id, key)] = job.task_id
        return QueueAdmission(AdmissionState.ACCEPTED, job.task_id)

    async def read_job(self, task_id: str) -> JobSnapshot | None:
        self._sweep()
        record = self.jobs.get(task_id)
        return _snapshot(record, self.storage_mode) if record else None

    async def read_job_by_operation(self, user_id: int, idempotency_key: str) -> JobSnapshot | None:
        identity = hashlib.sha256(f"{user_id}:{idempotency_key}".encode()).hexdigest()
        alias = self.identities.get(identity)
        now = self.clock()
        if not alias or alias[2] <= now:
            return None
        record = self.jobs.get(alias[0])
        if not record or (record.get("metadata_expires_at") and record["metadata_expires_at"] <= now):
            return None
        visible = dict(record)
        if record["payload_expires_at"] <= now or alias[0] not in self.payloads:
            visible.update(retry_eligible=False, retry_payload_available=False)
        return _snapshot(visible, "local")

    async def read_jobs_by_conflicts(
        self, user_id: int, lane: str, conflict_keys: tuple[str, ...]
    ) -> tuple[JobSnapshot, ...]:
        if lane not in LANE_POLICIES:
            raise QueueConflict("unknown_lane")
        snapshots = {}
        for key in _normalized_conflicts(conflict_keys):
            task_id = self.conflicts.get((lane, user_id, key))
            if task_id is None:
                continue
            record = self.jobs.get(task_id)
            if record and record["state"] in _ACTIVE:
                snapshots[task_id] = _snapshot(record, "local")
        return tuple(snapshots.values())

    async def stats(self, lane: str) -> dict[str, Any]:
        self._sweep()
        records = [m for m in self.jobs.values() if m["lane"] == lane]
        return {
            "nonterminal": sum(m["state"] in {"queued", "delayed", "running", "cancel_requested"} for m in records),
            "payload_bytes": sum(m["payload_bytes"] for m in records if m["task_id"] in self.payloads),
            "retrying": sum(m["state"] in {"queued", "delayed"} and m["attempts_started"] > 0 for m in records),
            "malformed_payloads": sum(m.get("reason_code") == "invalid_job_payload" for m in records),
            **{state.value: sum(m["state"] == state.value for m in records) for state in JobState},
            "oldest_age_seconds": max(
                (self.clock() - m["submitted_at"] for m in records if m["state"] in _ACTIVE), default=0
            ),
            "observed_at": self.clock(),
            "available": True,
            "scope": "process",
            "healthy_owners": sum(
                m["state"] in {"running", "cancel_requested"} and m["heartbeat_at"] + OWNER_LEASE_SECONDS > self.clock()
                for m in records
            ),
        }

    def _drop_payload(self, record):
        self.payloads.pop(record["task_id"], None)
        record["retry_payload_available"] = False
        record["retry_eligible"] = False

    def _terminal(self, record, state, reason=None, *, safe_retry=False):
        now = self.clock()
        record.update(
            state=state,
            reason_code=reason,
            finished_at=now,
            owner_token=None,
            metadata_expires_at=now + METADATA_RETENTION,
            revision=record["revision"] + 1,
        )
        for identity in record.get("identity_aliases", [record["identity"]]):
            previous = self.identities.get(identity)
            if previous:
                self.identities[identity] = (previous[0], previous[1], now + METADATA_RETENTION)
        self._release_conflicts(record)
        keep = safe_retry and record["retry_grants"] > 0 and record["task_id"] in self.payloads
        if keep:
            record.update(
                retry_eligible=True, retry_payload_available=True, payload_expires_at=now + RETRY_PAYLOAD_RETENTION
            )
        else:
            self._drop_payload(record)

    def _release_conflicts(self, record):
        for key in record.get("conflict_keys", ()):
            identity = (record["lane"], record["user_id"], key)
            if self.conflicts.get(identity) == record["task_id"]:
                self.conflicts.pop(identity, None)

    def _sweep(self):
        now = self.clock()
        for identity, value in list(self.identities.items()):
            if value[2] <= now:
                self.identities.pop(identity)
        for operation, value in list(self.operations.items()):
            if value[2] <= now:
                self.operations.pop(operation)
        for task_id, m in list(self.jobs.items()):
            if m.get("metadata_expires_at") and m["metadata_expires_at"] <= now:
                self._drop_payload(m)
                del self.jobs[task_id]
                continue
            if m["payload_expires_at"] <= now:
                self._drop_payload(m)
                if m["state"] in _ACTIVE:
                    uncertain = m["state"] in {"running", "cancel_requested"} and m["effectful"]
                    self._terminal(m, "unknown_delivery" if uncertain else "expired", "payload_expired")
            if m["state"] in _ACTIVE and m["deadline_at"] <= now:
                uncertain = m["state"] in {"running", "cancel_requested"} and m["effectful"]
                self._terminal(m, "unknown_delivery" if uncertain else "expired", "deadline_exceeded")
            elif m["state"] in {"running", "cancel_requested"} and m["heartbeat_at"] + OWNER_LEASE_SECONDS <= now:
                if m["effectful"]:
                    self._terminal(
                        m,
                        "unknown_delivery",
                        "document_storage_unconfirmed"
                        if m["kind"] == "document_processing"
                        else "interrupted_delivery",
                    )
                elif m["state"] == "cancel_requested":
                    self._terminal(m, "cancelled", "cancel_requested")
                elif m["attempts_started"] >= m["max_attempts"]:
                    self._terminal(m, "failed", "attempts_exhausted", safe_retry=True)
                elif now >= m.get("queue_deadline_at", m["deadline_at"]):
                    self._terminal(m, "expired", "deadline_exceeded", safe_retry=True)
                else:
                    m.update(
                        state="queued",
                        owner_token=None,
                        revision=m["revision"] + 1,
                        deadline_at=m.get("queue_deadline_at", m["deadline_at"]),
                    )
            elif m["state"] == "delayed" and m["not_before"] <= now:
                m.update(state="queued", revision=m["revision"] + 1)

    async def sweep(self):
        self._sweep()

    async def claim(self, lane: str, owner_token: str) -> JobClaim | None:
        self._sweep()
        if self.lanes.get(lane, {}).get("claim_paused"):
            return None
        running = [m for m in self.jobs.values() if m["state"] in {"running", "cancel_requested"}]
        if sum(m["lane"] == lane for m in running) >= LANE_WORKERS[lane]:
            return None
        for m in sorted(
            (m for m in self.jobs.values() if m["lane"] == lane and m["state"] == "queued"),
            key=lambda m: (-m["priority"], m["sequence"]),
        ):
            if m["attempts_started"] >= m["max_attempts"]:
                self._terminal(m, "failed", "attempts_exhausted", safe_retry=True)
                continue
            if m["image_work"] and any(active["image_work"] for active in running):
                continue
            if m["task_id"] not in self.payloads:
                self._terminal(m, "expired", "payload_expired")
                continue
            if m["lane"] == "daily" and m["user_id"] == 0:
                budget = m.get("execution_timeout_seconds") or (1800 if m["kind"].startswith("tarot.") else 600)
                m["deadline_at"] = self.clock() + budget + EXECUTION_SETTLEMENT_GRACE_SECONDS
                m["payload_expires_at"] = m["deadline_at"]
            m.update(
                state="running",
                owner_token=owner_token,
                started_at=self.clock(),
                heartbeat_at=self.clock(),
                attempts_started=m["attempts_started"] + 1,
                revision=m["revision"] + 1,
            )
            m.pop("finish_token", None)
            return JobClaim(
                _snapshot(m, "local"),
                None,
                m["user_id"],
                m["privacy_epoch"],
                owner_token,
                m["effectful"],
                m["image_work"],
            )
        return None

    async def load_payload(self, task_id: str, owner_token: str) -> str:
        self._sweep()
        m = self.jobs.get(task_id)
        if not m or m["state"] != "running" or m["owner_token"] != owner_token or task_id not in self.payloads:
            raise QueueConflict("payload_not_available")
        return self.payloads[task_id]

    def _owned(self, task_id, owner_token, revision):
        self._sweep()
        m = self.jobs.get(task_id)
        if (
            not m
            or m["state"] not in {"running", "cancel_requested"}
            or m["owner_token"] != owner_token
            or m["revision"] != revision
        ):
            raise QueueConflict("owner_or_revision_conflict")
        return m

    async def heartbeat(self, task_id: str, owner_token: str, expected_revision: int) -> JobSnapshot:
        self._sweep()
        m = self._owned(task_id, owner_token, expected_revision)
        m.update(heartbeat_at=self.clock())
        return _snapshot(m, "local")

    async def finish(
        self, task_id: str, owner_token: str, expected_revision: int, result: JobResult, *, delay_seconds: float = 0
    ) -> JobSnapshot:
        self._sweep()
        m = self.jobs.get(task_id)
        if m and m.get("finish_token") == owner_token:
            return _snapshot(m, "local")
        m = self._owned(task_id, owner_token, expected_revision)
        m["result_summary"] = {**m["result_summary"], **public_summary(result.result_summary)}
        if result.outcome is JobOutcome.UNKNOWN_DELIVERY:
            self._terminal(m, "unknown_delivery", result.reason_code)
        elif m["state"] == "cancel_requested":
            self._terminal(m, "cancelled", "cancel_requested")
        elif result.outcome is JobOutcome.RETRYABLE_FAILURE:
            if m["attempts_started"] >= m["max_attempts"]:
                self._terminal(m, "failed", result.reason_code or "attempts_exhausted", safe_retry=True)
            elif self.clock() + delay_seconds >= m.get("queue_deadline_at", m["deadline_at"]):
                self._terminal(m, "expired", "retry_after_deadline", safe_retry=True)
            else:
                m.update(
                    state="delayed" if delay_seconds > 0 else "queued",
                    not_before=self.clock() + delay_seconds,
                    reason_code=result.reason_code,
                    owner_token=None,
                    revision=m["revision"] + 1,
                    deadline_at=m.get("queue_deadline_at", m["deadline_at"]),
                    payload_expires_at=min(m.get("queue_deadline_at", m["deadline_at"]), m["submitted_at"] + 900)
                    if m["user_id"] > 0
                    else m.get("queue_deadline_at", m["deadline_at"]),
                )
        else:
            self._terminal(
                m,
                {
                    JobOutcome.SUCCEEDED: "succeeded",
                    JobOutcome.PERMANENT_FAILURE: "failed",
                    JobOutcome.UNKNOWN_DELIVERY: "unknown_delivery",
                }[result.outcome],
                result.reason_code,
            )
        m["finish_token"] = owner_token
        return _snapshot(m, "local")

    def _command(self, actor, operation_id, body):
        key = hashlib.sha256(f"{actor}:{operation_id}".encode()).hexdigest()
        fingerprint = json.dumps(body, sort_keys=True)
        old = self.operations.get(key)
        if old and old[0] != fingerprint:
            raise QueueConflict("operation_id_conflict")
        return key, fingerprint, old[1] if old else None

    async def update_progress(
        self, task_id: str, owner_token: str, expected_revision: int, summary: dict[str, int | str | bool]
    ) -> JobSnapshot:
        m = self._owned(task_id, owner_token, expected_revision)
        if m["state"] != "running":
            raise QueueConflict("cancel_requested")
        m["result_summary"] = {**m["result_summary"], **public_summary(summary)}
        return _snapshot(m, "local")

    async def request_cancel(
        self, task_id: str, *, expected_revision: int, actor: str, operation_id: str
    ) -> JobSnapshot:
        self._sweep()
        key, fp, previous = self._command(actor, operation_id, ["cancel", task_id, expected_revision])
        if previous:
            return _snapshot(self.jobs[task_id], "local")
        m = self.jobs.get(task_id)
        if not m or m["revision"] != expected_revision:
            raise QueueConflict("revision_conflict")
        if m["state"] in {"running", "cancel_requested"}:
            m.update(state="cancel_requested", revision=m["revision"] + 1, reason_code="cancel_requested")
        elif m["state"] in _ACTIVE:
            self._terminal(m, "cancelled", "cancel_requested")
        self.operations[key] = (fp, task_id, self.clock() + METADATA_RETENTION)
        return _snapshot(m, "local")

    async def retry_job(
        self, task_id: str, *, expected_revision: int, actor: str, operation_id: str, privacy_epoch: int | None
    ) -> QueueAdmission:
        self._sweep()
        key, fp, previous = self._command(actor, operation_id, ["retry", task_id, expected_revision])
        if previous:
            return QueueAdmission(AdmissionState.EXISTING, previous)
        m = self.jobs.get(task_id)
        if (
            not m
            or m["revision"] != expected_revision
            or not m["retry_eligible"]
            or m["state"] not in {"failed", "expired"}
            or task_id not in self.payloads
            or m["privacy_epoch"] != privacy_epoch
        ):
            raise QueueConflict("retry_not_available")
        child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        child = JobEnvelope(
            child_id,
            m["kind"],
            m["lane"],
            m["user_id"],
            self.payloads[task_id],
            f"retry:{key}",
            privacy_epoch=privacy_epoch,
            priority=m["priority"],
            effectful=m["effectful"],
            image_work=m["image_work"],
            parent_task_id=task_id,
            retry_grants=0,
            conflict_keys=tuple(m.get("conflict_keys", ())),
            public_summary=dict(m["result_summary"]),
            execution_timeout_seconds=m.get("execution_timeout_seconds"),
        )
        admission = self._admit(child, self.lanes[m["lane"]]["policy"], payload_credit=m["payload_bytes"])
        if admission.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING}:
            if admission.task_id != child_id:
                self.operations[key] = (fp, admission.task_id, self.clock() + METADATA_RETENTION)
                return admission
            m["retry_grants"] = 0
            self._drop_payload(m)
            self.operations[key] = (fp, child_id, self.clock() + METADATA_RETENTION)
        return admission

    async def private_record(self, task_id: str) -> dict[str, Any] | None:
        self._sweep()
        m = self.jobs.get(task_id)
        return dict(m) if m else None

    async def erase_user(self, user_id: int):
        identifiers = {task_id for task_id, m in self.jobs.items() if m["user_id"] == user_id}
        for task_id in identifiers:
            m = self.jobs.pop(task_id)
            self._drop_payload(m)
            self._release_conflicts(m)
            for identity in m.get("identity_aliases", [m["identity"]]):
                self.identities.pop(identity, None)
        self.operations = {key: value for key, value in self.operations.items() if value[1] not in identifiers}

    async def read_lane(self, lane: str) -> LaneSnapshot:
        if lane not in LANE_WORKERS:
            raise QueueConflict("unknown_lane")
        state = self.lanes.setdefault(
            lane, {"revision": 0, "admission_paused": False, "claim_paused": False, "policy": LANE_POLICIES[lane]}
        )
        return LaneSnapshot(
            lane,
            state["revision"],
            state["admission_paused"],
            state["claim_paused"],
            state["policy"],
            LANE_WORKERS[lane],
        )

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
        key, fp, old = self._command(
            actor, operation_id, ["lane", lane, expected_revision, admission_paused, claim_paused]
        )
        snapshot = await self.read_lane(lane)
        if old:
            return snapshot
        if snapshot.revision != expected_revision:
            raise QueueConflict("revision_conflict")
        self.lanes[lane].update(
            revision=expected_revision + 1, admission_paused=admission_paused, claim_paused=claim_paused
        )
        self.operations[key] = (fp, lane, self.clock() + METADATA_RETENTION)
        return await self.read_lane(lane)

    async def list_jobs(
        self, *, lane: str | None = None, state: JobState | None = None, cursor: str | None = None, limit: int = 50
    ) -> JobPage:
        self._sweep()
        if not 1 <= limit <= 100:
            raise QueueConflict("invalid_page_size")
        try:
            offset = int(cursor or "0")
            if offset < 0:
                raise ValueError
        except ValueError as error:
            raise QueueConflict("invalid_cursor") from error
        records = sorted(
            (
                m
                for m in self.jobs.values()
                if (lane is None or m["lane"] == lane) and (state is None or m["state"] == state.value)
            ),
            key=lambda m: m["sequence"],
        )
        selected = records[offset : offset + limit]
        return JobPage(
            tuple(_snapshot(m, "local") for m in selected),
            str(offset + limit) if len(records) > offset + limit else None,
        )


# Each script uses Redis TIME. The namespace is deliberately incompatible with
# the pre-v2 lists: startup never scans, claims or deletes legacy work.
from app.queue_lua import QUEUE_SCRIPT as _QUEUE_SCRIPT


class RedisQueueStore:
    storage_mode = "redis"

    def __init__(self, redis, *, namespace: str = QUEUE_NAMESPACE):
        self.redis = redis
        self.namespace = namespace

    async def _call(self, action: str, **body) -> Any:
        async with asyncio.timeout(5):
            raw = await self.redis.eval(
                _QUEUE_SCRIPT,
                1,
                self.namespace,
                json.dumps({"action": action, **body}, ensure_ascii=False, separators=(",", ":")),
            )
        result = json.loads(raw) if raw else None
        if isinstance(result, dict) and result.get("error"):
            raise QueueConflict(result["error"])
        return result

    async def _mutate(self, action: str, **body) -> Any:
        for attempt in range(2):
            try:
                return await self._call(action, **body)
            except QueueConflict:
                raise
            except Exception as error:
                if attempt:
                    raise QueueUnavailable("command_unconfirmed") from error

    async def admit(self, job: JobEnvelope, policy: QueuePolicy) -> QueueAdmission:
        if job.lane not in LANE_POLICIES:
            return QueueAdmission(AdmissionState.REJECTED, None, "unknown_lane", "redis")
        policy = _bounded_policy(job.lane, policy)
        serialized = asdict(job)
        serialized["conflict_keys"] = [
            hashlib.sha256(f"{job.user_id}:{key}".encode()).hexdigest() for key in job.conflict_keys
        ]
        body = {
            "job": serialized,
            "policy": policy.as_dict(),
            "identity": _identity(job),
            "fingerprint": _fingerprint(job),
        }
        try:
            result = await self._call("admit", **body)
        except Exception:
            try:
                result = await self._call("lookup_admission", **body)
            except Exception:
                return QueueAdmission(AdmissionState.UNKNOWN, job.task_id, "admission_unconfirmed", "redis")
        return QueueAdmission(
            AdmissionState(result["state"]), result.get("task_id"), result.get("reason_code"), "redis"
        )

    async def read_job(self, task_id: str) -> JobSnapshot | None:
        try:
            record = await self._call("read", task_id=task_id)
        except Exception as error:
            raise QueueUnavailable("status_unavailable") from error
        return _snapshot(record, "redis") if record else None

    async def read_job_by_operation(self, user_id: int, idempotency_key: str) -> JobSnapshot | None:
        identity = hashlib.sha256(f"{user_id}:{idempotency_key}".encode()).hexdigest()
        try:
            async with asyncio.timeout(5):
                raw = await self.redis.get(f"{self.namespace}:idem:{identity}")
                if not raw:
                    return None
                alias = json.loads(raw)
                raw_job = await self.redis.get(f"{self.namespace}:job:{alias['task_id']}")
                if not raw_job:
                    return None
                record = json.loads(raw_job)
                if record.get("retry_payload_available") and not await self.redis.exists(
                    f"{self.namespace}:payload:{alias['task_id']}"
                ):
                    record.update(retry_eligible=False, retry_payload_available=False)
                return _snapshot(record, "redis")
        except Exception as error:
            raise QueueUnavailable("status_unavailable") from error

    async def read_jobs_by_conflicts(
        self, user_id: int, lane: str, conflict_keys: tuple[str, ...]
    ) -> tuple[JobSnapshot, ...]:
        if lane not in LANE_POLICIES:
            raise QueueConflict("unknown_lane")
        keys = _normalized_conflicts(conflict_keys)
        snapshots = {}
        try:
            async with asyncio.timeout(5):
                for key in keys:
                    digest = hashlib.sha256(f"{user_id}:{key}".encode()).hexdigest()
                    task_id = await self.redis.get(f"{self.namespace}:conflict:{lane}:{digest}")
                    if not task_id:
                        continue
                    if isinstance(task_id, bytes):
                        task_id = task_id.decode("utf-8")
                    if task_id in snapshots:
                        continue
                    raw = await self.redis.get(f"{self.namespace}:job:{task_id}")
                    if raw:
                        record = json.loads(raw)
                        if record["state"] in _ACTIVE:
                            snapshots[task_id] = _snapshot(record, "redis")
        except Exception as error:
            raise QueueUnavailable("status_unavailable") from error
        return tuple(snapshots.values())

    async def stats(self, lane: str) -> dict[str, Any]:
        return await self._call("stats", lane=lane)

    async def sweep(self):
        await self._call("sweep")

    async def claim(self, lane: str, owner_token: str) -> JobClaim | None:
        # Never repeat an uncertain claim: its owner will be recovered conservatively.
        data = await self._call("claim", lane=lane, owner_token=owner_token, policy=LANE_POLICIES[lane].as_dict())
        if not data:
            return None
        m = data["record"]
        return JobClaim(
            _snapshot(m, "redis"),
            None,
            m["user_id"],
            m["privacy_epoch"],
            owner_token,
            m["effectful"],
            m["image_work"],
        )

    async def load_payload(self, task_id: str, owner_token: str) -> str:
        return await self._call("load", task_id=task_id, owner_token=owner_token)

    async def heartbeat(self, task_id: str, owner_token: str, expected_revision: int) -> JobSnapshot:
        data = await self._call(
            "heartbeat", task_id=task_id, owner_token=owner_token, expected_revision=expected_revision
        )
        return _snapshot(data, "redis")

    async def update_progress(
        self, task_id: str, owner_token: str, expected_revision: int, summary: dict[str, int | str | bool]
    ) -> JobSnapshot:
        clean = public_summary(summary)
        try:
            data = await self._call(
                "progress", task_id=task_id, owner_token=owner_token, expected_revision=expected_revision, summary=clean
            )
        except Exception:
            data = await self.private_record(task_id)
            if (
                not data
                or data.get("owner_token") != owner_token
                or data.get("state") != "running"
                or any(data.get("result_summary", {}).get(key) != value for key, value in clean.items())
            ):
                raise QueueConflict("progress_unconfirmed") from None
        return _snapshot(data, "redis")

    async def finish(
        self, task_id: str, owner_token: str, expected_revision: int, result: JobResult, *, delay_seconds: float = 0
    ) -> JobSnapshot:
        data = await self._mutate(
            "finish",
            task_id=task_id,
            owner_token=owner_token,
            expected_revision=expected_revision,
            result={
                "outcome": result.outcome.value,
                "reason_code": result.reason_code,
                "result_summary": public_summary(result.result_summary),
            },
            delay_seconds=delay_seconds,
        )
        return _snapshot(data, "redis")

    @staticmethod
    def _operation(actor, operation_id, body):
        return {
            "operation_key": hashlib.sha256(f"{actor}:{operation_id}".encode()).hexdigest(),
            "command_fingerprint": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
        }

    async def request_cancel(
        self, task_id: str, *, expected_revision: int, actor: str, operation_id: str
    ) -> JobSnapshot:
        args = self._operation(actor, operation_id, ["cancel", task_id, expected_revision])
        data = await self._mutate("cancel", task_id=task_id, expected_revision=expected_revision, **args)
        return _snapshot(data, "redis")

    async def private_record(self, task_id: str) -> dict[str, Any] | None:
        return await self._call("private", task_id=task_id)

    async def retry_job(
        self, task_id: str, *, expected_revision: int, actor: str, operation_id: str, privacy_epoch: int | None
    ) -> QueueAdmission:
        args = self._operation(actor, operation_id, ["retry", task_id, expected_revision])
        m = await self.private_record(task_id)
        if m is None:
            raise QueueConflict("retry_not_available")
        child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, args["operation_key"]))
        child_identity = hashlib.sha256(f"{m['user_id']}:{args['operation_key']}".encode()).hexdigest()
        data = await self._mutate(
            "retry",
            task_id=task_id,
            expected_revision=expected_revision,
            privacy_epoch=privacy_epoch,
            child_id=child_id,
            child_identity=child_identity,
            policy=LANE_POLICIES[m["lane"]].as_dict(),
            **args,
        )
        return QueueAdmission(AdmissionState(data["state"]), data.get("task_id"), data.get("reason_code"), "redis")

    async def erase_user(self, user_id: int):
        for _ in range(1000):
            result = await self._mutate("erase", user_id=user_id)
            if not result["remaining"]:
                return
        raise QueueUnavailable("erasure_incomplete")

    async def read_lane(self, lane: str) -> LaneSnapshot:
        if lane not in LANE_WORKERS:
            raise QueueConflict("unknown_lane")
        data = await self._call("lane", lane=lane, policy=LANE_POLICIES[lane].as_dict())
        return LaneSnapshot(
            lane,
            data["revision"],
            data["admission_paused"],
            data["claim_paused"],
            QueuePolicy(**data["policy"]),
            LANE_WORKERS[lane],
        )

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
        if lane not in LANE_WORKERS:
            raise QueueConflict("unknown_lane")
        args = self._operation(actor, operation_id, ["lane", lane, expected_revision, admission_paused, claim_paused])
        data = await self._mutate(
            "set_lane",
            lane=lane,
            expected_revision=expected_revision,
            admission_paused=admission_paused,
            claim_paused=claim_paused,
            policy=LANE_POLICIES[lane].as_dict(),
            **args,
        )
        return LaneSnapshot(
            lane,
            data["revision"],
            data["admission_paused"],
            data["claim_paused"],
            QueuePolicy(**data["policy"]),
            LANE_WORKERS[lane],
        )

    async def list_jobs(
        self, *, lane: str | None = None, state: JobState | None = None, cursor: str | None = None, limit: int = 50
    ) -> JobPage:
        if not 1 <= limit <= 100:
            raise QueueConflict("invalid_page_size")
        try:
            offset = int(cursor or "0")
            if offset < 0:
                raise ValueError
        except ValueError as error:
            raise QueueConflict("invalid_cursor") from error
        data = await self._call("list", lane=lane, state=state.value if state else None, offset=offset, limit=limit)
        return JobPage(tuple(_snapshot(m, "redis") for m in data["items"]), data.get("next_cursor"))
