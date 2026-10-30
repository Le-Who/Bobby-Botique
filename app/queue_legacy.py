"""Explicit v1 wire compatibility and operator-controlled legacy inspection.

No v2 worker executes these lists. No automatic legacy recovery is performed.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from itertools import count
from typing import Any

from app import database as db
from app.observability.context import export_job_context, restore_job_context
from app.observability.events import emit, record_exception
from app.queue_contracts import JobOutcome, JobResult, QueueDisposition, normalize_job_result
from app.utils.json_compat import json


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN_DELIVERY = "unknown_delivery"
    DISPOSITION_UNCONFIRMED = "disposition_unconfirmed"


class TaskPriority(Enum):
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


@dataclass
class Task:
    """Задача в очереди"""

    id: str
    user_id: int
    task_type: str
    data: dict[str, Any]
    priority: TaskPriority
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    attempts_started: int = 0
    revision: int = 0
    observability_schema_version: int = 1
    observability_context: dict[str, Any] = field(default_factory=dict)

    @property
    def max_attempts(self) -> int:
        """Legacy max_retries has always bounded total executions, not extras."""
        return max(1, self.max_retries)


# ── Redis key constants ─────────────────────────────────────────────────
_QUEUE_PREFIX = "gemaibotv2:queue"  # List per priority: gemaibotv2:queue:4, :3, :2, :1
_PROCESSING_KEY = "gemaibotv2:processing"  # Legacy unowned processing list; preserved during rolling upgrades
_OWNERS_KEY = "gemaibotv2:queue:owners"
_LEASE_PREFIX = "gemaibotv2:queue:lease"
_LEASE_SECONDS = 60
_LEASE_RENEW_INTERVAL = 15.0
_REDIS_TIMEOUT = 5.0
_TASK_HASH_PREFIX = "gemaibotv2:task"  # Hash per task: gemaibotv2:task:{id}
_IDLE_POLL_TIMEOUT = 30.0  # seconds — fallback poll when Event not fired

# Claims and disposition are fenced by a unique queue-instance lease. A crashed
# owner's processing list persists without TTL and is recovered atomically only
# after its lease expires. Recovery never deletes a whole shared list.
_ACQUIRE_LEASE = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
    redis.call('SADD', KEYS[2], ARGV[1])
    return 1
end
return 0
"""
_RENEW_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_CLAIM_TASK = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return nil end
return redis.call('RPOPLPUSH', KEYS[2], KEYS[3])
"""
_FINISH_TASK = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local removed = redis.call('LREM', KEYS[2], 1, ARGV[2])
if removed == 1 and ARGV[3] ~= '' then
    redis.call('LPUSH', KEYS[3], ARGV[3])
end
return removed
"""
_RECOVER_TASK = """
if ARGV[3] ~= 'legacy' and redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local removed = redis.call('LREM', KEYS[2], 1, ARGV[1])
if removed == 1 and ARGV[2] ~= '' then redis.call('LPUSH', KEYS[3], ARGV[2]) end
return removed
"""
_REMOVE_OWNER = """
if redis.call('EXISTS', KEYS[1]) == 0 and redis.call('LLEN', KEYS[2]) == 0 then
    return redis.call('SREM', KEYS[3], ARGV[1])
end
return 0
"""


def _queue_key(priority: TaskPriority) -> str:
    return f"{_QUEUE_PREFIX}:{priority.value}"


def _task_payload(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "user_id": task.user_id,
        "task_type": task.task_type,
        "data": task.data,
        "priority": task.priority.value,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "result": task.result,
        "error": task.error,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "attempts_started": task.attempts_started,
        "observability_schema_version": task.observability_schema_version,
        "observability_context": task.observability_context,
    }


def _task_to_json(task: Task) -> str:
    """Serialize a Task to text JSON for compatibility with existing callers."""
    return json.dumps(_task_payload(task))


def _task_to_json_bytes(task: Task) -> bytes:
    """Serialize a Task directly to UTF-8 bytes for Redis writes."""
    return json.dumps_bytes(_task_payload(task))


def _task_from_json(raw: str | bytes) -> Task:
    """Deserialize a Task from Redis JSON."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    d = json.loads(raw)
    return Task(
        id=d["id"],
        user_id=d["user_id"],
        task_type=d["task_type"],
        data=d["data"],
        priority=TaskPriority(d["priority"]),
        status=TaskStatus(d["status"]),
        created_at=datetime.fromisoformat(d["created_at"]),
        started_at=datetime.fromisoformat(d["started_at"]) if d.get("started_at") else None,
        completed_at=datetime.fromisoformat(d["completed_at"]) if d.get("completed_at") else None,
        result=d.get("result"),
        error=d.get("error"),
        retry_count=d.get("retry_count", 0),
        max_retries=d.get("max_retries", 3),
        attempts_started=d.get("attempts_started", d.get("retry_count", 0)),
        observability_schema_version=d.get("observability_schema_version", 1),
        observability_context=d.get("observability_context") or {},
    )


async def _legacy_processing_keys(redis):
    cursor = 0
    for _ in range(1000):
        cursor, keys = await redis.scan(cursor, match=f"{_PROCESSING_KEY}*", count=100)
        for key in keys:
            yield key.decode() if isinstance(key, bytes) else key
        if int(cursor) == 0:
            return
    raise RuntimeError("legacy_scan_incomplete")


async def inspect_legacy(redis):
    """Read bounded counts only. Legacy payloads require an explicit drained cutover."""
    ready = sum([await redis.llen(f"{_QUEUE_PREFIX}:{priority}") for priority in (1, 2, 3, 4)])
    processing = 0
    inspected = 0
    async for key in _legacy_processing_keys(redis):
        processing += await redis.llen(key)
        inspected += 1
        if inspected >= 128:
            break
    return {
        "available": True,
        "ready": ready,
        "processing": processing,
        "counts_bounded": inspected >= 128,
        "reason_code": "legacy_drain_required" if ready or processing else None,
    }


async def erase_legacy_user(redis, user_id: int):
    """Erase exact matching private payloads, without executing or moving jobs."""
    keys = [f"{_QUEUE_PREFIX}:{priority}" for priority in (1, 2, 3, 4)]
    keys.extend([key async for key in _legacy_processing_keys(redis)])
    for key in keys:
        offset = 0
        while True:
            rows = await redis.lrange(key, offset, offset + 49)
            if not rows:
                break
            removed = 0
            for raw in rows:
                try:
                    task = _task_from_json(raw)
                except ValueError, KeyError, TypeError, UnicodeDecodeError:
                    # A malformed entry cannot be attributed safely; do not print it.
                    raise RuntimeError("legacy_payload_requires_operator_erasure") from None
                if task.user_id == user_id:
                    removed += await redis.lrem(key, 1, raw)
            offset += len(rows) - removed


async def recover_legacy_processing(redis, *, confirm_drained: bool = False):
    """Explicit v1 maintenance; unsafe or malformed records remain untouched."""
    if not confirm_drained:
        raise ValueError("legacy_drain_required")
    recovered = 0
    async for key in _legacy_processing_keys(redis):
        owner = key.removeprefix(f"{_PROCESSING_KEY}:") if key != _PROCESSING_KEY else ""
        for raw in await redis.lrange(key, 0, -1):
            try:
                task = _task_from_json(raw)
                safe = task.task_type == "cleanup_metrics" and task.attempts_started + 1 < task.max_attempts
            except ValueError, KeyError, TypeError, AttributeError:
                continue
            if not safe:
                # A drained worker is not evidence that an external effect did
                # not happen. Recovery must neither replay nor discard it.
                continue
            task.attempts_started += 1
            task.retry_count = task.attempts_started
            task.status = TaskStatus.PENDING
            recovered += await redis.eval(
                _RECOVER_TASK,
                3,
                f"{_LEASE_PREFIX}:{owner}",
                key,
                _queue_key(task.priority),
                raw,
                _task_to_json_bytes(task),
                "legacy",
            )
    return recovered
