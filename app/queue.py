import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from app import database as db
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


# ── Redis key constants ─────────────────────────────────────────────────
_QUEUE_PREFIX = "gemaibotv2:queue"  # List per priority: gemaibotv2:queue:4, :3, :2, :1
_PROCESSING_KEY = "gemaibotv2:processing"  # List of task JSONs currently being processed
_TASK_HASH_PREFIX = "gemaibotv2:task"  # Hash per task: gemaibotv2:task:{id}
_IDLE_POLL_TIMEOUT = 30.0  # seconds — fallback poll when Event not fired


def _queue_key(priority: TaskPriority) -> str:
    return f"{_QUEUE_PREFIX}:{priority.value}"


def _task_to_json(task: Task) -> str:
    """Serialize a Task to JSON for Redis storage."""
    return json.dumps(
        {
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
        }
    )


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
        self.running = False
        self._lock = asyncio.Lock()
        self._task_handlers: dict[str, Callable] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._metrics_scheduler_task: asyncio.Task | None = None
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

    async def _recover_processing_tasks(self):
        """On startup, move tasks stuck in processing list back to their queues."""
        redis = _get_redis()
        if not redis:
            return

        try:
            # Get all items in processing list
            stuck = await redis.lrange(_PROCESSING_KEY, 0, -1)
            if not stuck:
                return

            logging.warning("Recovering %d tasks from processing list (likely crashed)", len(stuck))
            for raw in stuck:
                try:
                    task = _task_from_json(raw)
                    task.status = TaskStatus.PENDING
                    task.retry_count += 1
                    # Re-enqueue
                    await redis.lpush(_queue_key(task.priority), _task_to_json(task).encode())
                    # Update in-memory cache
                    self.tasks[task.id] = task
                except Exception as e:
                    logging.error("Failed to recover task: %s", e, exc_info=True)

            # Clear processing list
            await redis.delete(_PROCESSING_KEY)
            logging.info("Recovery complete: %d tasks re-queued", len(stuck))
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
        )

        async with self._lock:
            self.tasks[task_id] = task

        if self._use_redis:
            try:
                redis = _get_redis()
                if redis:
                    await redis.lpush(_queue_key(priority), _task_to_json(task).encode())
                    self._work_available.set()  # Wake idle workers
                    logging.info("Added task %s (type=%s, user=%s) to Redis queue", task_id, task_type, user_id)
                    return task_id
            except Exception as e:
                logging.error("Redis enqueue failed, falling back to memory: %s", e, exc_info=True)

        # Fallback to in-memory queue
        try:
            await asyncio.wait_for(self._fallback_queue.put((-priority.value, task_id)), timeout=2.0)
        except TimeoutError:
            async with self._lock:
                self.tasks.pop(task_id, None)
            logging.warning("Task queue put timeout. Rejecting task %s", task_id)
            return ""

        self._work_available.set()  # Wake idle workers
        logging.info("Added task %s (type=%s, user=%s) to in-memory queue", task_id, task_type, user_id)
        return task_id

    async def get_task_status(self, task_id: str) -> Task | None:
        """Получает статус задачи"""
        async with self._lock:
            return self.tasks.get(task_id)

    async def cancel_task(self, task_id: str, user_id: int) -> bool:
        """Отменяет задачу"""
        async with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.user_id != user_id:
                return False

            if task.status in [TaskStatus.PENDING, TaskStatus.RUNNING]:
                task.status = TaskStatus.CANCELLED
                task.completed_at = datetime.now(tz=UTC)
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
                    # Poll priority lists from highest (4=URGENT) to lowest (1=LOW)
                    for prio_val in (4, 3, 2, 1):
                        raw = await redis.rpoplpush(f"{_QUEUE_PREFIX}:{prio_val}", _PROCESSING_KEY)
                        if raw:
                            task = _task_from_json(raw)
                            # Preserve original bytes for ack/nack (task fields mutate later)
                            original = raw if isinstance(raw, bytes) else raw.encode()
                            return task, original
                except Exception as e:
                    logging.error("Redis dequeue failed: %s", e, exc_info=True)
                return None, None

        # Fallback: in-memory queue
        try:
            _, task_id = self._fallback_queue.get_nowait()
            async with self._lock:
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
                    await redis.lrem(_PROCESSING_KEY, 1, original_json)
                except Exception as e:
                    logging.debug("Redis ack cleanup: %s", e)

    async def _nack_task(self, task: Task, original_json: bytes | None):
        """Return a failed task to the queue for retry."""
        if self._use_redis:
            redis = _get_redis()
            if redis:
                try:
                    # Remove from processing list using original bytes
                    if original_json:
                        await redis.lrem(_PROCESSING_KEY, 1, original_json)
                    # Re-enqueue with updated state
                    task.status = TaskStatus.PENDING
                    await redis.lpush(_queue_key(task.priority), _task_to_json(task).encode())
                    self._work_available.set()  # Wake workers for retry
                except Exception as e:
                    logging.error("Redis nack failed: %s", e, exc_info=True)
        else:
            try:
                await self._fallback_queue.put((-task.priority.value + 1, task.id))
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
                    async with self._lock:
                        cached = self.tasks.get(task.id)
                        if cached and cached.status == TaskStatus.CANCELLED:
                            await self._ack_task(original_json)
                            continue

                        # Update status
                        task.status = TaskStatus.RUNNING
                        task.started_at = datetime.now(tz=UTC)
                        self.tasks[task.id] = task

                    logging.info("Worker %s processing task %s", worker_name, task.id)

                    try:
                        result = await self._execute_task(task)

                        async with self._lock:
                            task.status = TaskStatus.COMPLETED
                            task.completed_at = datetime.now(tz=UTC)
                            task.result = result
                            self.tasks[task.id] = task

                        await self._ack_task(original_json)
                        logging.info("Task %s completed successfully", task.id)

                    except Exception as e:
                        logging.error("Task %s failed: %s", task.id, e, exc_info=True)

                        async with self._lock:
                            task.error = str(e)
                            task.retry_count += 1

                            if task.retry_count < task.max_retries:
                                await self._nack_task(task, original_json)
                            else:
                                task.status = TaskStatus.FAILED
                                task.completed_at = datetime.now(tz=UTC)
                                await self._ack_task(original_json)
                            self.tasks[task.id] = task

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

                async with self._lock:
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
        if self._use_redis:
            redis = _get_redis()
            if redis:
                try:
                    for prio_val in (4, 3, 2, 1):
                        redis_queue_size += await redis.llen(f"{_QUEUE_PREFIX}:{prio_val}")
                except Exception:
                    pass

        async with self._lock:
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
                "queue_size": redis_queue_size or self._fallback_queue.qsize(),
                "active_workers": len([w for w in self.workers if not w.done()]),
                "backend": "redis" if self._use_redis else "memory",
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
