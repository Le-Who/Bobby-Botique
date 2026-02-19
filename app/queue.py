import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Callable, List, Coroutine
from dataclasses import dataclass
from enum import Enum
import uuid

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
    data: Dict[str, Any]
    priority: TaskPriority
    status: TaskStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3

class TaskQueue:
    """Очередь задач для длительных операций"""
    
    def __init__(self, max_workers: int = 3):
        self.max_workers = max_workers
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.tasks: Dict[str, Task] = {}
        self.workers: List[asyncio.Task] = []
        self.running = False
        self._lock = asyncio.Lock()
        self._task_handlers: Dict[str, Callable] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._metrics_scheduler_task: Optional[asyncio.Task] = None
        
        # Инициализируем обработчики задач
        self._init_task_handlers()
    
    def _init_task_handlers(self):
        """Инициализирует обработчики для разных типов задач"""
        
        self._task_handlers = {
            'document_processing': self._handle_document_processing,
            'cleanup_metrics': self._handle_cleanup_metrics,
        }
    
    async def start(self):
        """Запускает очередь задач"""
        if self.running:
            return
        
        self.running = True
        logging.info(f"Starting task queue with {self.max_workers} workers")
        
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
        
        # Добавляем сигналы завершения для каждого воркера
        for _ in range(self.max_workers):
            await self.queue.put((float('-inf'), None))
        
        # Ждем завершения всех воркеров
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

        # Отменяем служебные задачи
        await self._cancel_background_task("_cleanup_task")
        await self._cancel_background_task("_metrics_scheduler_task")
        
        logging.info("Task queue stopped")

    def _start_background_task(
        self,
        task_ref: Optional[asyncio.Task],
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        task_name: str,
    ) -> asyncio.Task:
        """Запускает фоновую задачу с защитой от повторного старта."""
        if task_ref and not task_ref.done():
            logging.debug("Background task '%s' already running", task_name)
            return task_ref

        return asyncio.create_task(coro_factory())

    async def _cancel_background_task(self, attr_name: str):
        """Отменяет и ожидает завершение фоновой задачи по имени атрибута."""
        task = getattr(self, attr_name, None)
        if not task:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        setattr(self, attr_name, None)
    
    async def add_task(self, user_id: int, task_type: str, data: Dict[str, Any], 
                      priority: TaskPriority = TaskPriority.NORMAL) -> str:
        """Добавляет задачу в очередь"""
        task_id = str(uuid.uuid4())
        
        task = Task(
            id=task_id,
            user_id=user_id,
            task_type=task_type,
            data=data,
            priority=priority,
            status=TaskStatus.PENDING,
            created_at=datetime.now()
        )
        
        async with self._lock:
            self.tasks[task_id] = task
        
        # Добавляем в очередь (приоритет - это отрицательное число, чтобы высокий приоритет был первым)
        await self.queue.put((-priority.value, task_id))
        
        logging.info(f"Added task {task_id} of type {task_type} for user {user_id}")
        return task_id
    
    async def get_task_status(self, task_id: str) -> Optional[Task]:
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
        logging.info(f"Worker {worker_name} started")
        
        while True:
            try:
                # Получаем задачу из очереди
                priority, task_id = await self.queue.get()
                
                try:
                    # Проверяем сигнал завершения
                    if task_id is None:
                        break

                    async with self._lock:
                        task = self.tasks.get(task_id)
                        if not task or task.status == TaskStatus.CANCELLED:
                            continue

                        task.status = TaskStatus.RUNNING
                        task.started_at = datetime.now()
                    
                    logging.info(f"Worker {worker_name} processing task {task_id}")
                    
                    # Выполняем задачу
                    try:
                        result = await self._execute_task(task)
                        
                        async with self._lock:
                            task.status = TaskStatus.COMPLETED
                            task.completed_at = datetime.now()
                            task.result = result

                        logging.info(f"Task {task_id} completed successfully")

                    except Exception as e:
                        logging.error(f"Task {task_id} failed: {e}")

                        async with self._lock:
                            task.error = str(e)
                            task.retry_count += 1

                            if task.retry_count < task.max_retries:
                                task.status = TaskStatus.PENDING
                                # Повторно добавляем в очередь с более низким приоритетом
                                await self.queue.put((-task.priority.value + 1, task_id))
                            else:
                                task.status = TaskStatus.FAILED
                                task.completed_at = datetime.now()
                finally:
                    self.queue.task_done()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Worker {worker_name} error: {e}")
                await asyncio.sleep(1)
        
        logging.info(f"Worker {worker_name} stopped")
    
    async def _execute_task(self, task: Task) -> Dict[str, Any]:
        """Выполняет задачу"""
        handler = self._task_handlers.get(task.task_type)
        if not handler:
            raise ValueError(f"Unknown task type: {task.task_type}")
        
        # Вызываем обработчик задачи
        result = await handler(**task.data)
        return result
    
    async def _handle_document_processing(self, **kwargs) -> Dict[str, Any]:
        """Обработчик для обработки документов"""
        try:
            from app.document_processor import process_uploaded_document
            
            file_data = kwargs.get('file_data')
            filename = kwargs.get('filename')
            user_id = kwargs.get('user_id')
            
            if not all([file_data, filename, user_id]):
                return {"status": "failed", "error": "Missing required parameters"}
            
            # Обрабатываем документ
            result = await process_uploaded_document(file_data, filename, user_id)
            
            if result.get("error"):
                return {"status": "failed", "error": result["error"]}
            
            return {
                "status": "completed",
                "pages": result.get("pages", 0),
                "text_length": result.get("text_length", 0),
                "paragraphs": result.get("paragraphs", 0),
                "tables": result.get("tables", 0)
            }
            
        except Exception as e:
            logging.error(f"Error in document processing task: {e}")
            return {"status": "failed", "error": str(e)}
    
    async def _handle_cleanup_metrics(self, **kwargs) -> Dict[str, Any]:
        """Обработчик для очистки старых метрик"""
        try:
            # Удаляем метрики старше 30 дней
            await db.db_query("""
                DELETE FROM metrics 
                WHERE metric_date < CURRENT_DATE - INTERVAL '30 days'
            """)
            
            # Удаляем старые ошибки (старше 7 дней)
            await db.db_query("""
                DELETE FROM error_logs 
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
            """)
            
            return {"status": "completed", "message": "Old metrics cleaned up"}
        except Exception as e:
            logging.error(f"Error cleaning up metrics: {e}")
            return {"status": "failed", "error": str(e)}
    
    async def _cleanup_old_tasks(self):
        """Очищает старые задачи"""
        while self.running:
            try:
                await asyncio.sleep(3600)  # Проверяем каждый час
                
                cutoff_time = datetime.now() - timedelta(days=7)  # Удаляем задачи старше недели
                
                async with self._lock:
                    tasks_to_remove = [
                        task_id for task_id, task in self.tasks.items()
                        if task.completed_at and task.completed_at < cutoff_time
                    ]
                    
                    for task_id in tasks_to_remove:
                        del self.tasks[task_id]
                    
                    if tasks_to_remove:
                        logging.info(f"Cleaned up {len(tasks_to_remove)} old tasks")
                        
            except Exception as e:
                logging.error(f"Error in cleanup task: {e}")
    
    async def _schedule_metrics_cleanup(self):
        """Планирует автоматическую очистку метрик"""
        while self.running:
            try:
                await asyncio.sleep(86400)  # Ждем 24 часа
                
                # Добавляем задачу очистки метрик
                await self.add_task(
                    user_id=0,  # Системная задача
                    task_type='cleanup_metrics',
                    data={},
                    priority=TaskPriority.LOW
                )
                
                logging.info("Scheduled metrics cleanup task")
                
            except Exception as e:
                logging.error(f"Error in metrics cleanup scheduler: {e}")
    
    async def get_queue_stats(self) -> Dict[str, Any]:
        """Возвращает статистику очереди"""
        async with self._lock:
            total_tasks = len(self.tasks)
            pending_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.PENDING)
            running_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.RUNNING)
            completed_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.COMPLETED)
            failed_tasks = sum(1 for task in self.tasks.values() if task.status == TaskStatus.FAILED)
            
            return {
                'total_tasks': total_tasks,
                'pending_tasks': pending_tasks,
                'running_tasks': running_tasks,
                'completed_tasks': completed_tasks,
                'failed_tasks': failed_tasks,
                'queue_size': self.queue.qsize(),
                'active_workers': len([w for w in self.workers if not w.done()])
            }

# Глобальный экземпляр очереди задач
task_queue = TaskQueue()

async def start_task_queue():
    """Запускает очередь задач"""
    await task_queue.start()

async def stop_task_queue():
    """Останавливает очередь задач"""
    await task_queue.stop()

async def add_background_task(user_id: int, task_type: str, data: Dict[str, Any], 
                            priority: TaskPriority = TaskPriority.NORMAL) -> str:
    """Добавляет задачу в фоновую очередь"""
    return await task_queue.add_task(user_id, task_type, data, priority)

async def get_task_status(task_id: str) -> Optional[Task]:
    """Получает статус задачи"""
    return await task_queue.get_task_status(task_id)

async def cancel_user_task(task_id: str, user_id: int) -> bool:
    """Отменяет задачу пользователя"""
    return await task_queue.cancel_task(task_id, user_id) 
