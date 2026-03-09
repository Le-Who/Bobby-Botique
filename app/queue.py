import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from app import database as db


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


class TaskQueue:
    """Очередь задач для длительных операций"""

    def __init__(self, max_workers: int = 3):
        self.max_workers = max_workers
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=100)  # prevent unbounded queue growth
        self.tasks: dict[str, Task] = {}
        self.workers: list[asyncio.Task] = []
        self.running = False
        self._lock = asyncio.Lock()
        self._task_handlers: dict[str, Callable] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._metrics_scheduler_task: asyncio.Task | None = None
        # Initialize обработчики задач
        self._init_task_handlers()

    def _init_task_handlers(self):
        """Инициализирует обработчики для разных типов задач"""

        self._task_handlers = {
            "document_processing": self._handle_document_processing,
            "cleanup_metrics": self._handle_cleanup_metrics,
        }

    async def start(self):
        """Запускает очередь задач"""
        if self.running:
            return

        self.running = True
        logging.info("Starting task queue with %s workers", self.max_workers)

        # Запускаем воркеры
        for i in range(self.max_workers):
            worker = asyncio.create_task(self._worker(f"worker-{i}"))
            self.workers.append(worker)

        # Запускаем задачу очистки старых задач
        self._cleanup_task = self._start_background_task(
            self._cleanup_task,
            self._cleanup_old_tasks,
            "task queue cleanup",
        )

        # Запускаем автоматическую очистку метрик (каждые 24 часа)
        self._metrics_scheduler_task = self._start_background_task(
            self._metrics_scheduler_task,
            self._schedule_metrics_cleanup,
            "metrics cleanup scheduler",
        )

    async def stop(self):
        """Останавливает очередь задач"""
        if not self.running:
            return

        self.running = False
        logging.info("Stopping task queue...")
        # Cancel all workers to prevent deadlock when queue is full
        for worker in self.workers:
            if not worker.done():
                worker.cancel()

        # Ждем завершения всех воркеров
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

        # Отменяем служебные задачи
        await self._cancel_background_task("_cleanup_task")
        await self._cancel_background_task("_metrics_scheduler_task")

        logging.info("Task queue stopped")

    def _start_background_task(
        self,
        task_ref: asyncio.Task | None,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        task_name: str,
    ) -> asyncio.Task:
        """Запускает фоновую задачу с защитой от повторного старта."""
        from app.utils.background_tasks import start_background_task

        return start_background_task(task_ref, coro_factory, task_name)

    async def _cancel_background_task(self, attr_name: str):
        """Отменяет и ожидает завершение фоновой задачи по имени атрибута."""
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
            created_at=datetime.now(),
        )

        async with self._lock:
            if self.queue.full():
                logging.warning("Task queue is full. Rejecting task %s", task_id)
                return ""
            self.tasks[task_id] = task

        # Add в queue (onоритет - это отрицательное число, чтобы высокий onоритет был первым)
        try:
            await asyncio.wait_for(self.queue.put((-priority.value, task_id)), timeout=2.0)
        except TimeoutError:
            async with self._lock:
                self.tasks.pop(task_id, None)
            logging.warning("Task queue put timeout. Rejecting task %s", task_id)
            return ""

        logging.info("Added task %s of type %s for user %s", task_id, task_type, user_id)
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
                task.completed_at = datetime.now()
                return True

            return False

    async def _worker(self, worker_name: str):
        """Воркер для обработки задач"""
        logging.info("Worker %s started", worker_name)
        while True:
            try:
                # Get задачу from очереди
                _, task_id = await self.queue.get()

                try:
                    # Check сигнал завершения
                    if task_id is None:
                        break

                    async with self._lock:
                        task = self.tasks.get(task_id)
                        if not task or task.status == TaskStatus.CANCELLED:
                            continue

                        task.status = TaskStatus.RUNNING
                        task.started_at = datetime.now()

                    logging.info("Worker %s processing task %s", worker_name, task_id)

                    # Execute задачу
                    try:
                        result = await self._execute_task(task)

                        async with self._lock:
                            task.status = TaskStatus.COMPLETED
                            task.completed_at = datetime.now()
                            task.result = result

                        logging.info("Task %s completed successfully", task_id)

                    except Exception as e:
                        logging.error("Task %s failed: %s", task_id, e, exc_info=True)

                        async with self._lock:
                            task.error = str(e)
                            task.retry_count += 1

                            if task.retry_count < task.max_retries:
                                task.status = TaskStatus.PENDING
                                # Повторно добавляем в queue с более нfromким onоритетом
                                await self.queue.put((-task.priority.value + 1, task_id))
                            else:
                                task.status = TaskStatus.FAILED
                                task.completed_at = datetime.now()
                finally:
                    self.queue.task_done()

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

        # Call обработчик задачи
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

            # Process document
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
            # Delete metrics старше 30 дней
            await db.db_query("""
                DELETE FROM metrics
                WHERE metric_date < CURRENT_DATE - INTERVAL '30 days'
            """)

            # Delete old ошибки (старше 7 дней)
            await db.db_query("""
                DELETE FROM error_logs
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
            """)

            return {"status": "completed", "message": "Old metrics cleaned up"}
        except Exception as e:
            logging.error("Error cleaning up metrics: %s", e, exc_info=True)
            return {"status": "failed", "error": str(e)}

    async def _cleanup_old_tasks(self):
        """Очищает старые задачи"""
        while self.running:
            try:
                await asyncio.sleep(3600)  # Check каждый час

                cutoff_time = datetime.now() - timedelta(days=7)  # Delete задачи старше недели

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
                await asyncio.sleep(86400)  # Ждем 24 часа

                # Add задачу очистки метрик
                await self.add_task(
                    user_id=0,  # Системная задача
                    task_type="cleanup_metrics",
                    data={},
                    priority=TaskPriority.LOW,
                )

                logging.info("Scheduled metrics cleanup task")

            except Exception as e:
                logging.error("Error in metrics cleanup scheduler: %s", e, exc_info=True)

    async def get_queue_stats(self) -> dict[str, Any]:
        """Возвращает статистику очереди"""
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
                "queue_size": self.queue.qsize(),
                "active_workers": len([w for w in self.workers if not w.done()]),
            }


# Глобальный экземпляр очереди задач
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
