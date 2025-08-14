"""
Персистентная очередь задач на базе Redis (опциональная)
"""
import json
import logging
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import uuid

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logging.warning("Redis not available. Persistent queue will be disabled.")

from .config import settings
from .queue import Task, TaskStatus, TaskPriority


class RedisQueue:
    """Персистентная очередь задач на Redis"""
    
    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or getattr(settings, 'REDIS_URL', 'redis://localhost:6379')
        self.redis_client: Optional[redis.Redis] = None
        self.queue_key = "gemaibot:task_queue"
        self.tasks_key = "gemaibot:tasks"
        self.processing_key = "gemaibot:processing"
        
    async def connect(self) -> bool:
        """Подключается к Redis"""
        if not REDIS_AVAILABLE:
            return False
            
        try:
            self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
            # Проверяем соединение
            await self.redis_client.ping()
            logging.info(f"Connected to Redis at {self.redis_url}")
            return True
        except Exception as e:
            logging.error(f"Failed to connect to Redis: {e}")
            return False
    
    async def disconnect(self):
        """Отключается от Redis"""
        if self.redis_client:
            await self.redis_client.close()
    
    async def enqueue(self, task: Task) -> bool:
        """Добавляет задачу в очередь"""
        if not self.redis_client:
            return False
            
        try:
            # Сериализуем задачу
            task_data = {
                'id': task.id,
                'user_id': task.user_id,
                'task_type': task.task_type,
                'data': task.data,
                'priority': task.priority.value,
                'status': task.status.value,
                'created_at': task.created_at.isoformat(),
                'max_retries': task.max_retries,
                'retry_count': task.retry_count
            }
            
            # Сохраняем задачу
            await self.redis_client.hset(self.tasks_key, task.id, json.dumps(task_data))
            
            # Добавляем в очередь с приоритетом
            score = -task.priority.value  # Высокий приоритет = меньший score
            await self.redis_client.zadd(self.queue_key, {task.id: score})
            
            logging.debug(f"Enqueued task {task.id} to Redis queue")
            return True
            
        except Exception as e:
            logging.error(f"Failed to enqueue task {task.id}: {e}")
            return False
    
    async def dequeue(self, timeout: int = 1) -> Optional[Task]:
        """Извлекает задачу из очереди"""
        if not self.redis_client:
            return None
            
        try:
            # Блокирующий pop с timeout
            result = await self.redis_client.bzpopmin(self.queue_key, timeout=timeout)
            if not result:
                return None
            
            queue_name, task_id, score = result
            
            # Получаем данные задачи
            task_data_json = await self.redis_client.hget(self.tasks_key, task_id)
            if not task_data_json:
                logging.warning(f"Task data not found for ID {task_id}")
                return None
            
            task_data = json.loads(task_data_json)
            
            # Восстанавливаем объект Task
            task = Task(
                id=task_data['id'],
                user_id=task_data['user_id'],
                task_type=task_data['task_type'],
                data=task_data['data'],
                priority=TaskPriority(task_data['priority']),
                status=TaskStatus(task_data['status']),
                created_at=datetime.fromisoformat(task_data['created_at']),
                max_retries=task_data.get('max_retries', 3),
                retry_count=task_data.get('retry_count', 0)
            )
            
            # Помечаем как обрабатываемую
            await self.redis_client.hset(self.processing_key, task_id, json.dumps(task_data))
            
            logging.debug(f"Dequeued task {task_id} from Redis queue")
            return task
            
        except Exception as e:
            logging.error(f"Failed to dequeue task: {e}")
            return None
    
    async def complete_task(self, task: Task, result: Dict[str, Any] = None) -> bool:
        """Помечает задачу как завершённую"""
        if not self.redis_client:
            return False
            
        try:
            # Обновляем статус
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now()
            task.result = result
            
            task_data = {
                'id': task.id,
                'user_id': task.user_id,
                'task_type': task.task_type,
                'data': task.data,
                'priority': task.priority.value,
                'status': task.status.value,
                'created_at': task.created_at.isoformat(),
                'completed_at': task.completed_at.isoformat(),
                'result': task.result,
                'max_retries': task.max_retries,
                'retry_count': task.retry_count
            }
            
            # Обновляем в хранилище
            await self.redis_client.hset(self.tasks_key, task.id, json.dumps(task_data))
            
            # Удаляем из processing
            await self.redis_client.hdel(self.processing_key, task.id)
            
            logging.debug(f"Completed task {task.id}")
            return True
            
        except Exception as e:
            logging.error(f"Failed to complete task {task.id}: {e}")
            return False
    
    async def fail_task(self, task: Task, error: str) -> bool:
        """Помечает задачу как неудачную или возвращает в очередь для retry"""
        if not self.redis_client:
            return False
            
        try:
            task.retry_count += 1
            task.error = error
            
            if task.retry_count < task.max_retries:
                # Возвращаем в очередь с пониженным приоритетом
                task.status = TaskStatus.PENDING
                score = -task.priority.value + task.retry_count  # Снижаем приоритет
                await self.redis_client.zadd(self.queue_key, {task.id: score})
                logging.debug(f"Retrying task {task.id} (attempt {task.retry_count}/{task.max_retries})")
            else:
                # Помечаем как неудачную
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now()
                logging.debug(f"Failed task {task.id} after {task.retry_count} attempts")
            
            task_data = {
                'id': task.id,
                'user_id': task.user_id,
                'task_type': task.task_type,
                'data': task.data,
                'priority': task.priority.value,
                'status': task.status.value,
                'created_at': task.created_at.isoformat(),
                'completed_at': task.completed_at.isoformat() if task.completed_at else None,
                'error': task.error,
                'max_retries': task.max_retries,
                'retry_count': task.retry_count
            }
            
            # Обновляем в хранилище
            await self.redis_client.hset(self.tasks_key, task.id, json.dumps(task_data))
            
            # Удаляем из processing
            await self.redis_client.hdel(self.processing_key, task.id)
            
            return True
            
        except Exception as e:
            logging.error(f"Failed to handle task failure {task.id}: {e}")
            return False
    
    async def get_task_status(self, task_id: str) -> Optional[Task]:
        """Получает статус задачи"""
        if not self.redis_client:
            return None
            
        try:
            task_data_json = await self.redis_client.hget(self.tasks_key, task_id)
            if not task_data_json:
                return None
            
            task_data = json.loads(task_data_json)
            
            return Task(
                id=task_data['id'],
                user_id=task_data['user_id'],
                task_type=task_data['task_type'],
                data=task_data['data'],
                priority=TaskPriority(task_data['priority']),
                status=TaskStatus(task_data['status']),
                created_at=datetime.fromisoformat(task_data['created_at']),
                completed_at=datetime.fromisoformat(task_data['completed_at']) if task_data.get('completed_at') else None,
                result=task_data.get('result'),
                error=task_data.get('error'),
                max_retries=task_data.get('max_retries', 3),
                retry_count=task_data.get('retry_count', 0)
            )
            
        except Exception as e:
            logging.error(f"Failed to get task status {task_id}: {e}")
            return None
    
    async def get_queue_stats(self) -> Dict[str, Any]:
        """Получает статистику очереди"""
        if not self.redis_client:
            return {}
            
        try:
            # Размер очереди
            queue_size = await self.redis_client.zcard(self.queue_key)
            
            # Количество обрабатываемых задач
            processing_count = await self.redis_client.hlen(self.processing_key)
            
            # Общее количество задач
            total_tasks = await self.redis_client.hlen(self.tasks_key)
            
            # Статистика по статусам (требует сканирования)
            status_counts = {status.value: 0 for status in TaskStatus}
            
            # Сканируем все задачи для подсчета статусов
            all_tasks = await self.redis_client.hgetall(self.tasks_key)
            for task_data_json in all_tasks.values():
                try:
                    task_data = json.loads(task_data_json)
                    status = task_data.get('status', 'unknown')
                    if status in status_counts:
                        status_counts[status] += 1
                except:
                    continue
            
            return {
                'queue_size': queue_size,
                'processing_count': processing_count,
                'total_tasks': total_tasks,
                'pending_tasks': status_counts.get('pending', 0),
                'running_tasks': status_counts.get('running', 0),
                'completed_tasks': status_counts.get('completed', 0),
                'failed_tasks': status_counts.get('failed', 0),
                'cancelled_tasks': status_counts.get('cancelled', 0)
            }
            
        except Exception as e:
            logging.error(f"Failed to get queue stats: {e}")
            return {}
    
    async def cleanup_old_tasks(self, older_than_days: int = 7) -> int:
        """Очищает старые завершённые задачи"""
        if not self.redis_client:
            return 0
            
        try:
            cutoff_date = datetime.now() - timedelta(days=older_than_days)
            cleaned_count = 0
            
            all_tasks = await self.redis_client.hgetall(self.tasks_key)
            for task_id, task_data_json in all_tasks.items():
                try:
                    task_data = json.loads(task_data_json)
                    status = task_data.get('status')
                    completed_at_str = task_data.get('completed_at')
                    
                    # Удаляем только завершённые/неудачные/отменённые задачи старше cutoff_date
                    if status in ['completed', 'failed', 'cancelled'] and completed_at_str:
                        completed_at = datetime.fromisoformat(completed_at_str)
                        if completed_at < cutoff_date:
                            await self.redis_client.hdel(self.tasks_key, task_id)
                            cleaned_count += 1
                            
                except Exception:
                    continue
            
            logging.info(f"Cleaned up {cleaned_count} old tasks from Redis queue")
            return cleaned_count
            
        except Exception as e:
            logging.error(f"Failed to cleanup old tasks: {e}")
            return 0


# Глобальный экземпляр Redis очереди
redis_queue = RedisQueue() if REDIS_AVAILABLE else None


async def init_redis_queue() -> bool:
    """Инициализирует Redis очередь если доступна"""
    if not redis_queue:
        return False
    
    if await redis_queue.connect():
        logging.info("Redis queue initialized successfully")
        return True
    else:
        logging.warning("Redis queue initialization failed")
        return False


async def cleanup_redis_queue():
    """Очищает Redis очередь"""
    if redis_queue:
        await redis_queue.disconnect()
