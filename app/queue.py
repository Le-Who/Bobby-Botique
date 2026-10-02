import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from app import database as db
from app.observability.context import export_job_context, restore_job_context
from app.observability.events import emit, record_exception
from app.utils.json_compat import json


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    observability_schema_version: int = 1
    observability_context: dict[str, Any] = field(default_factory=dict)


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
if removed == 1 then redis.call('LPUSH', KEYS[3], ARGV[2]) end
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
        observability_schema_version=d.get("observability_schema_version", 1),
        observability_context=d.get("observability_context") or {},
    )


def _get_redis():
    """Lazy import to avoid circular dependencies with cache.py."""
    from app.cache import redis_client

    return redis_client


class TaskQueue:
    """Очередь задач с Redis-бэкендом для durability.

    Falls back to in-memory asyncio.Queue when Redis is unavailable.
    """

    def __init__(self, max_workers: int = 3):
        self.max_workers = max_workers
        self.tasks: dict[str, Task] = {}
        self.workers: list[asyncio.Task] = []
        self._executions: dict[str, asyncio.Task] = {}
        self._redis_executions: set[str] = set()
        self.running = False
        self._task_handlers: dict[str, Callable] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._metrics_scheduler_task: asyncio.Task | None = None
        self._lease_task: asyncio.Task | None = None
        self._owner_id = uuid.uuid4().hex
        self._lease_acquired = False
        self._lease_lost = False
        self._lease_lock = asyncio.Lock()
        # In-memory fallback queue (used when Redis unavailable)
        self._fallback_queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=100)
        self._use_redis = False
        # Event-driven wakeup: workers sleep on this Event instead of polling
        self._work_available = asyncio.Event()
        # Initialize task handlers
        self._init_task_handlers()

    def _init_task_handlers(self):
        """Инициализирует обработчики для разных типов задач"""
        self._task_handlers = {
            "document_processing": self._handle_document_processing,
            "cleanup_metrics": self._handle_cleanup_metrics,
            "deferred_ai_response": self._handle_deferred_ai_response,
        }

    async def start(self):
        """Запускает очередь задач"""
        if self.running:
            return

        self.running = True

        # Check Redis availability
        redis = _get_redis()
        self._use_redis = redis is not None
        if self._use_redis:
            logging.info("Starting task queue with Redis backend (%s workers)", self.max_workers)
            await self._recover_processing_tasks()
        else:
            logging.info("Starting task queue with in-memory fallback (%s workers)", self.max_workers)

        # Start workers
        for i in range(self.max_workers):
            worker = asyncio.create_task(self._worker(f"worker-{i}"))
            self.workers.append(worker)

        # Start background cleanup
        self._cleanup_task = self._start_background_task(
            self._cleanup_task,
            self._cleanup_old_tasks,
            "task queue cleanup",
        )

        # Start metrics cleanup scheduler
        self._metrics_scheduler_task = self._start_background_task(
            self._metrics_scheduler_task,
            self._schedule_metrics_cleanup,
            "metrics cleanup scheduler",
        )

    @property
    def _processing_key(self) -> str:
        return f"{_PROCESSING_KEY}:{self._owner_id}"

    @property
    def _lease_key(self) -> str:
        return f"{_LEASE_PREFIX}:{self._owner_id}"

    async def _ensure_lease(self, redis) -> bool:
        async with self._lease_lock:
            if self._lease_lost:
                if self._redis_executions:
                    return False
                # Resume admission under a new fenced identity only after every
                # handler from the lost lease has stopped. The old list remains
                # available for expired-owner recovery.
                self._owner_id = uuid.uuid4().hex
                self._lease_acquired = False
                self._lease_lost = False
            if not self._lease_acquired:
                async with asyncio.timeout(_REDIS_TIMEOUT):
                    self._lease_acquired = bool(
                        await redis.eval(
                            _ACQUIRE_LEASE, 2, self._lease_key, _OWNERS_KEY, self._owner_id, _LEASE_SECONDS
                        )
                    )
                if self._lease_acquired and self.running:
                    self._lease_task = self._start_background_task(
                        self._lease_task, self._maintain_lease, "task queue ownership lease"
                    )
            return self._lease_acquired

    async def _maintain_lease(self):
        while self.running:
            await asyncio.sleep(_LEASE_RENEW_INTERVAL)
            try:
                redis = _get_redis()
                if redis is None:
                    raise ConnectionError("Redis unavailable")
                async with asyncio.timeout(_REDIS_TIMEOUT):
                    renewed = await redis.eval(_RENEW_LEASE, 1, self._lease_key, self._owner_id, _LEASE_SECONDS)
                if not renewed:
                    raise ConnectionError("Queue ownership lease expired")
            except Exception as error:
                # Fail closed on the first renewal failure, well before the TTL.
                # Never resurrect a lost owner while its old handlers may execute.
                self._lease_lost = True
                for task_id in tuple(self._redis_executions):
                    execution = self._executions.get(task_id)
                    if execution is not None:
                        execution.cancel()
                logging.warning("Queue ownership lease lost: %s", type(error).__name__)
                return
            await self._recover_processing_tasks()

    async def _recover_processing_tasks(self, *, recover_legacy: bool = False):
        try:
            async with asyncio.timeout(_REDIS_TIMEOUT):
                await self._recover_processing_tasks_inner(recover_legacy=recover_legacy)
        except TimeoutError:
            logging.warning("Queue recovery timed out; retained entries will be retried")

    async def _recover_processing_tasks_inner(self, *, recover_legacy: bool = False):
        """Recover expired owners, preserving live replicas and malformed entries.

        ``recover_legacy=True`` is a controlled one-time migration operation:
        callers must first stop/drain ALL workers using the old shared list.
        Unowned legacy entries cannot prove that their old worker has stopped.
        """
        redis = _get_redis()
        if not redis:
            return

        try:
            legacy_count = await redis.llen(_PROCESSING_KEY)
            if legacy_count and not recover_legacy:
                logging.warning(
                    "Preserving %d legacy processing tasks; controlled recovery requires draining old workers",
                    legacy_count,
                )
            owners = await redis.smembers(_OWNERS_KEY)
            sources = [(owner.decode() if isinstance(owner, bytes) else owner, False) for owner in owners]
            if recover_legacy:
                sources.append(("", True))
            recovered = 0
            for owner, legacy in sources:
                lease_key = f"{_LEASE_PREFIX}:{owner}"
                processing_key = _PROCESSING_KEY if legacy else f"{_PROCESSING_KEY}:{owner}"
                if not legacy and await redis.exists(lease_key):
                    continue
                for raw in await redis.lrange(processing_key, 0, -1):
                    try:
                        task = _task_from_json(raw)
                        task.status = TaskStatus.PENDING
                        task.started_at = None
                        task.retry_count += 1
                        moved = await redis.eval(
                            _RECOVER_TASK,
                            3,
                            lease_key,
                            processing_key,
                            _queue_key(task.priority),
                            raw,
                            _task_to_json_bytes(task),
                            "legacy" if legacy else "owned",
                        )
                        if not moved:
                            continue
                        recovered += 1
                        self.tasks[task.id] = task
                        self._work_available.set()
                        emit(
                            "job.retry_scheduled",
                            level="warning",
                            operation="job.recover",
                            job_id=task.id,
                            task_type=task.task_type,
                            retry_count=task.retry_count,
                            reason_code="process_recovery",
                            backend="redis",
                            delay_seconds=0,
                        )
                    except Exception as error:
                        logging.warning("Processing recovery retained entry: %s", type(error).__name__)
                if not legacy:
                    await redis.eval(_REMOVE_OWNER, 3, lease_key, processing_key, _OWNERS_KEY, owner)
            if recovered:
                logging.info("Recovery complete: %d tasks re-queued", recovered)
        except Exception as e:
            logging.error("Task recovery failed: %s", e, exc_info=True)

    async def stop(self):
        """Останавливает очередь задач"""
        if not self.running:
            return

        self.running = False
        logging.info("Stopping task queue...")
        # Cancel all workers
        for worker in self.workers:
            if not worker.done():
                worker.cancel()

        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

        await self._cancel_background_task("_lease_task")
        if self._lease_acquired:
            redis = _get_redis()
            if redis is not None:
                try:
                    async with asyncio.timeout(_REDIS_TIMEOUT):
                        await redis.eval(_RELEASE_LEASE, 1, self._lease_key, self._owner_id)
                        await self._recover_processing_tasks()
                except Exception as error:
                    logging.warning("Queue shutdown retains processing tasks: %s", type(error).__name__)
            self._lease_acquired = False
            self._owner_id = uuid.uuid4().hex
            self._lease_lost = False

        # Cancel background tasks
        await self._cancel_background_task("_cleanup_task")
        await self._cancel_background_task("_metrics_scheduler_task")

        logging.info("Task queue stopped")

    def _start_background_task(
        self,
        task_ref: asyncio.Task | None,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        task_name: str,
    ) -> asyncio.Task:
        """Starts a background task with duplicate-start protection."""
        from app.utils.background_tasks import start_background_task

        return start_background_task(task_ref, coro_factory, task_name)

    async def _cancel_background_task(self, attr_name: str):
        """Cancels and awaits a background task by attribute name."""
        from app.utils.background_tasks import cancel_background_task

        await cancel_background_task(self, attr_name)

    async def add_task(
        self,
        user_id: int,
        task_type: str,
        data: dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> str:
        """Добавляет задачу в очередь"""
        task_id = str(uuid.uuid4())

        task = Task(
            id=task_id,
            user_id=user_id,
            task_type=task_type,
            data=data,
            priority=priority,
            status=TaskStatus.PENDING,
            created_at=datetime.now(tz=UTC),
            observability_context=export_job_context(),
        )

        self.tasks[task_id] = task

        if self._use_redis:
            try:
                redis = _get_redis()
                if redis:
                    await redis.lpush(_queue_key(priority), _task_to_json_bytes(task))
                    self._work_available.set()  # Wake idle workers
                    emit(
                        "job.enqueued",
                        operation="job.enqueue",
                        job_id=task_id,
                        task_type=task_type,
                        backend="redis",
                        priority=priority.value,
                    )
                    logging.info("Added task %s (type=%s, user=%s) to Redis queue", task_id, task_type, user_id)
                    return task_id
            except Exception as e:
                logging.error("Redis enqueue failed, falling back to memory: %s", e, exc_info=True)

        # Fallback to in-memory queue
        try:
            await asyncio.wait_for(self._fallback_queue.put((-priority.value, task_id)), timeout=2.0)
        except TimeoutError:
            self.tasks.pop(task_id, None)
            emit(
                "job.capacity_rejected",
                level="warning",
                operation="job.enqueue",
                job_id=task_id,
                task_type=task_type,
                reason_code="fallback_queue_timeout",
                backend="memory",
            )
            return ""

        self._work_available.set()  # Wake idle workers
        emit(
            "job.enqueued",
            operation="job.enqueue",
            job_id=task_id,
            task_type=task_type,
            backend="memory",
            priority=priority.value,
        )
        logging.info("Added task %s (type=%s, user=%s) to in-memory queue", task_id, task_type, user_id)
        return task_id

    async def get_task_status(self, task_id: str) -> Task | None:
        """Получает статус задачи"""
        return self.tasks.get(task_id)

    async def cancel_task(self, task_id: str, user_id: int) -> bool:
        """Отменяет задачу"""
        task = self.tasks.get(task_id)
        if not task or task.user_id != user_id:
            return False

        if task.status in [TaskStatus.PENDING, TaskStatus.RUNNING]:
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now(tz=UTC)
            execution = self._executions.get(task_id)
            if execution is not None:
                execution.cancel()
            emit(
                "job.cancelled",
                level="warning",
                operation="job.cancel",
                task_id=task.id,
                task_type=task.task_type,
                previous_status="pending" if task.started_at is None else "running",
            )
            return True

        return False

    async def _dequeue_task(self) -> tuple[Task | None, bytes | None]:
        """Dequeue a task from Redis (priority-ordered) or fallback queue.

        Returns:
            (task, original_json_bytes) — original_json_bytes is the raw Redis
            value needed by _ack_task/_nack_task for exact LREM matching.
        """
        if self._use_redis:
            redis = _get_redis()
            if redis:
                try:
                    if await self._ensure_lease(redis):
                        for prio_val in (4, 3, 2, 1):
                            async with asyncio.timeout(_REDIS_TIMEOUT):
                                raw = await redis.eval(
                                    _CLAIM_TASK,
                                    3,
                                    self._lease_key,
                                    f"{_QUEUE_PREFIX}:{prio_val}",
                                    self._processing_key,
                                    self._owner_id,
                                )
                            if raw:
                                task = _task_from_json(raw)
                                original = raw if isinstance(raw, bytes) else raw.encode()
                                return task, original
                except Exception as e:
                    logging.error("Redis dequeue failed: %s", e, exc_info=True)

        # Failed Redis writes can leave local work even while the client exists.
        # Drain it after an empty/failed Redis read; local tasks have no Redis ACK.
        try:
            _, task_id = self._fallback_queue.get_nowait()
            return self.tasks.get(task_id), None
        except asyncio.QueueEmpty:
            return None, None

    async def _ack_task(self, original_json: bytes | None):
        """Acknowledge task completion — remove from processing list.

        Args:
            original_json: The exact bytes returned by rpoplpush at dequeue time.
                          Using the original (not re-serialized) bytes guarantees
                          LREM will find and remove the correct entry.
        """
        if original_json and self._use_redis:
            redis = _get_redis()
            if redis:
                try:
                    async with asyncio.timeout(_REDIS_TIMEOUT):
                        await redis.eval(
                            _FINISH_TASK,
                            3,
                            self._lease_key,
                            self._processing_key,
                            "",
                            self._owner_id,
                            original_json,
                            "",
                        )
                except Exception as e:
                    logging.debug("Redis ack cleanup: %s", e)

    async def _nack_task(self, task: Task, original_json: bytes | None):
        """Return a failed task to the queue for retry."""
        task.status = TaskStatus.PENDING
        if original_json is not None and self._use_redis:
            redis = _get_redis()
            if redis:
                try:
                    async with asyncio.timeout(_REDIS_TIMEOUT):
                        await redis.eval(
                            _FINISH_TASK,
                            3,
                            self._lease_key,
                            self._processing_key,
                            _queue_key(task.priority),
                            self._owner_id,
                            original_json,
                            _task_to_json_bytes(task),
                        )
                    self._work_available.set()  # Wake workers for retry
                except Exception as e:
                    logging.error("Redis nack failed: %s", e, exc_info=True)
        else:
            try:
                await asyncio.wait_for(self._fallback_queue.put((-task.priority.value, task.id)), timeout=2.0)
                self._work_available.set()  # Wake workers for retry
            except Exception:
                pass

    async def _worker(self, worker_name: str):
        """Worker that processes tasks from Redis or fallback queue.

        Uses asyncio.Event for wakeup instead of constant RPOP polling.
        Workers sleep until add_task() signals _work_available, or until
        a 30-second fallback timeout fires (catches crash-recovery and
        external Redis enqueues).
        """
        logging.info("Worker %s started", worker_name)
        while True:
            try:
                # Wait for work signal or fallback timeout
                try:
                    await asyncio.wait_for(self._work_available.wait(), timeout=_IDLE_POLL_TIMEOUT)
                except TimeoutError:
                    pass  # Periodic fallback poll

                # Drain all available tasks before going back to sleep
                while True:
                    task, original_json = await self._dequeue_task()
                    if task is None:
                        self._work_available.clear()  # Nothing left — reset signal
                        break

                    # Check if cancelled
                    cached = self.tasks.get(task.id)
                    if cached and cached.status == TaskStatus.CANCELLED:
                        await self._ack_task(original_json)
                        continue

                    # Update status
                    task.status = TaskStatus.RUNNING
                    task.started_at = datetime.now(tz=UTC)
                    self.tasks[task.id] = task

                    execution_id = uuid.uuid4().hex
                    execution_started = datetime.now(tz=UTC)
                    with restore_job_context(
                        task.observability_context,
                        task_id=task.id,
                        execution_id=execution_id,
                    ):
                        emit(
                            "job.started",
                            operation="job.execute",
                            job_id=task.id,
                            task_type=task.task_type,
                            worker_name=worker_name,
                            retry_count=task.retry_count,
                            max_retries=task.max_retries,
                            queue_wait_ms=round((execution_started - task.created_at).total_seconds() * 1000, 2),
                        )
                        try:
                            execution = asyncio.create_task(self._execute_task(task), name=f"queue-job-{task.id}")
                            self._executions[task.id] = execution
                            if original_json is not None:
                                self._redis_executions.add(task.id)
                            try:
                                result = await execution
                            finally:
                                self._executions.pop(task.id, None)
                                self._redis_executions.discard(task.id)

                            if task.status == TaskStatus.CANCELLED:
                                await self._ack_task(original_json)
                                continue

                            task.status = TaskStatus.COMPLETED
                            task.completed_at = datetime.now(tz=UTC)
                            task.result = result
                            self.tasks[task.id] = task

                            await self._ack_task(original_json)
                            business_status = result.get("status") if isinstance(result, dict) else None
                            emit(
                                "job.finished",
                                level="warning" if business_status == "failed" else "info",
                                operation="job.execute",
                                job_id=task.id,
                                outcome="failed" if business_status == "failed" else "succeeded",
                                execution_outcome="returned",
                                business_outcome=business_status or "unknown",
                                task_type=task.task_type,
                                duration_ms=round(
                                    (task.completed_at - execution_started).total_seconds() * 1000,
                                    2,
                                ),
                            )

                        except asyncio.CancelledError:
                            owner = asyncio.current_task()
                            if owner is not None and owner.cancelling():
                                raise
                            if original_json is not None and self._lease_lost:
                                # Another replica may recover this entry; do not ACK
                                # or publish cancellation under an expired owner.
                                task.status = TaskStatus.PENDING
                                continue
                            task.status = TaskStatus.CANCELLED
                            task.completed_at = datetime.now(tz=UTC)
                            await self._ack_task(original_json)
                            emit(
                                "job.finished",
                                operation="job.execute",
                                job_id=task.id,
                                task_type=task.task_type,
                                outcome="cancelled",
                                execution_outcome="cancelled",
                            )
                        except Exception as e:
                            error_id = record_exception(
                                "job.execution_failed",
                                e,
                                operation="job.execute",
                                fields={
                                    "job_id": task.id,
                                    "task_type": task.task_type,
                                    "retry_count": task.retry_count,
                                },
                            )

                            task.error = f"{type(e).__name__}:{error_id}"
                            task.retry_count += 1

                            if task.retry_count < task.max_retries:
                                await self._nack_task(task, original_json)
                                retry_disposition = "scheduled"
                            else:
                                task.status = TaskStatus.FAILED
                                task.completed_at = datetime.now(tz=UTC)
                                await self._ack_task(original_json)
                                retry_disposition = "exhausted"
                            self.tasks[task.id] = task
                            emit(
                                "job.finished",
                                level="error",
                                operation="job.execute",
                                job_id=task.id,
                                outcome="failed",
                                execution_outcome="raised",
                                business_outcome="failed",
                                task_type=task.task_type,
                                error_id=error_id,
                                retry_disposition=retry_disposition,
                                retry_count=task.retry_count,
                                duration_ms=round(
                                    (datetime.now(tz=UTC) - execution_started).total_seconds() * 1000,
                                    2,
                                ),
                            )
                            if retry_disposition == "scheduled":
                                emit(
                                    "job.retry_scheduled",
                                    level="warning",
                                    operation="job.retry",
                                    job_id=task.id,
                                    task_type=task.task_type,
                                    retry_count=task.retry_count,
                                    max_retries=task.max_retries,
                                    delay_seconds=0,
                                    error_id=error_id,
                                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Worker %s error: %s", worker_name, e, exc_info=True)
                await asyncio.sleep(1)

        logging.info("Worker %s stopped", worker_name)

    async def _execute_task(self, task: Task) -> dict[str, Any]:
        """Выполняет задачу"""
        handler = self._task_handlers.get(task.task_type)
        if not handler:
            raise ValueError(f"Unknown task type: {task.task_type}")

        from app.runtime_settings.lifecycle import runtime_settings_scope

        async with runtime_settings_scope():
            result = await handler(**task.data)
        return result

    async def _handle_document_processing(self, **kwargs) -> dict[str, Any]:
        """Обработчик для обработки документов"""
        try:
            from app.document_processor import process_uploaded_document

            file_data = kwargs.get("file_data")
            filename = kwargs.get("filename")
            user_id = kwargs.get("user_id")

            if not all([file_data, filename, user_id]):
                return {"status": "failed", "error": "Missing required parameters"}

            result = await process_uploaded_document(file_data, str(filename), int(user_id))  # type: ignore[arg-type]

            if result.get("error"):
                return {"status": "failed", "error": result["error"]}

            return {
                "status": "completed",
                "pages": result.get("pages", 0),
                "text_length": result.get("text_length", 0),
                "paragraphs": result.get("paragraphs", 0),
                "tables": result.get("tables", 0),
            }

        except Exception as e:
            logging.error("Error in document processing task: %s", e, exc_info=True)
            return {"status": "failed", "error": str(e)}

    async def _handle_cleanup_metrics(self, **kwargs) -> dict[str, Any]:
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

            return {"status": "completed", "message": "Old metrics cleaned up"}
        except Exception as e:
            logging.error("Error cleaning up metrics: %s", e, exc_info=True)
            return {"status": "failed", "error": str(e)}

    async def _handle_deferred_ai_response(self, **kwargs) -> dict[str, Any]:
        """Handler for deferred AI generation retry (Plan §5)."""
        from app.deferred_response import handle_deferred_ai_response

        return await handle_deferred_ai_response(**kwargs)

    async def _cleanup_old_tasks(self):
        """Очищает старые задачи"""
        while self.running:
            try:
                await asyncio.sleep(3600)  # Check каждый час

                cutoff_time = datetime.now(tz=UTC) - timedelta(days=7)

                tasks_to_remove = [
                    task_id
                    for task_id, task in self.tasks.items()
                    if task.completed_at and task.completed_at < cutoff_time
                ]

                for task_id in tasks_to_remove:
                    del self.tasks[task_id]

                if tasks_to_remove:
                    logging.info("Cleaned up %s old tasks", len(tasks_to_remove))

            except Exception as e:
                logging.error("Error in cleanup task: %s", e, exc_info=True)

    async def _schedule_metrics_cleanup(self):
        """Планирует автоматическую очистку метрик"""
        while self.running:
            try:
                await asyncio.sleep(86400)  # 24 hours

                await self.add_task(
                    user_id=0,  # System task
                    task_type="cleanup_metrics",
                    data={},
                    priority=TaskPriority.LOW,
                )

                logging.info("Scheduled metrics cleanup task")

            except Exception as e:
                logging.error("Error in metrics cleanup scheduler: %s", e, exc_info=True)

    async def get_queue_stats(self) -> dict[str, Any]:
        """Возвращает статистику очереди"""
        redis_queue_size = 0
        redis_available: bool | None = None
        if self._use_redis:
            redis_available = False
            redis = _get_redis()
            if redis:
                try:
                    async with asyncio.timeout(5.0):
                        for prio_val in (4, 3, 2, 1):
                            redis_queue_size += await redis.llen(f"{_QUEUE_PREFIX}:{prio_val}")
                    redis_available = True
                except Exception:
                    pass
        local_queue_size = self._fallback_queue.qsize()
        backend = "memory"
        if self._use_redis:
            backend = "redis_unavailable" if not redis_available else "mixed" if local_queue_size else "redis"

        total_tasks = len(self.tasks)
        pending_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.PENDING)
        running_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.RUNNING)
        completed_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.COMPLETED)
        failed_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.FAILED)

        return {
            "total_tasks": total_tasks,
            "pending_tasks": pending_tasks,
            "running_tasks": running_tasks,
            "completed_tasks": completed_tasks,
            "failed_tasks": failed_tasks,
            "queue_size": None if redis_available is False else redis_queue_size + local_queue_size,
            "local_queue_size": local_queue_size,
            "redis_queue_size": redis_queue_size if redis_available else None,
            "redis_available": redis_available,
            "execution_scope": "process",
            "active_workers": len([w for w in self.workers if not w.done()]),
            "backend": backend,
        }


# Global task queue instance
task_queue = TaskQueue()


async def start_task_queue():
    """Запускает очередь задач"""
    await task_queue.start()


async def stop_task_queue():
    """Останавливает очередь задач"""
    await task_queue.stop()


async def add_background_task(
    user_id: int,
    task_type: str,
    data: dict[str, Any],
    priority: TaskPriority = TaskPriority.NORMAL,
) -> str:
    """Добавляет задачу в фоновую очередь"""
    return await task_queue.add_task(user_id, task_type, data, priority)


async def get_task_status(task_id: str) -> Task | None:
    """Получает статус задачи"""
    return await task_queue.get_task_status(task_id)


async def cancel_user_task(task_id: str, user_id: int) -> bool:
    """Отменяет задачу пользователя"""
    return await task_queue.cancel_task(task_id, user_id)
