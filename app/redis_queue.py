"""
Персистентная очередь задач на базе Redis (поддержка локального и внешних сервисов)
"""
import json
import logging
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import uuid
import os

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logging.warning("Redis not available. Persistent queue will be disabled.")

from .config import settings
from .types import Task, TaskStatus, TaskPriority


class RedisQueue:
    """Персистентная очередь задач на Redis (локальный или внешний сервис)"""

    def __init__(self, redis_url: str = None):
        # Приоритет конфигурации: env -> settings -> default
        self.redis_url = (
            redis_url or
            os.getenv('REDIS_URL') or
            getattr(settings, 'REDIS_URL', None) or
            'redis://localhost:6379'
        )

        # Определяем тип Redis сервиса
        self.service_type = self._detect_service_type()
        self.redis_client: Optional[redis.Redis] = None

        # Ключи с префиксом для изоляции
        self.queue_key = "gemaibot:task_queue"
        self.tasks_key = "gemaibot:tasks"
        self.processing_key = "gemaibot:processing"

        # Настройки для внешних сервисов
        self.connection_timeout = 10
        self.socket_timeout = 30
        self.retry_on_timeout = True

        logging.info(f"Redis Queue initialized for {self.service_type}: {self.redis_url}")

    def _detect_service_type(self) -> str:
        """Определяет тип Redis сервиса по URL"""
        if 'upstash.com' in self.redis_url:
            return "Upstash"
        elif 'redis.ondigitalocean.com' in self.redis_url:
            return "DigitalOcean Managed Redis"
        elif 'redis.cache.windows.net' in self.redis_url:
            return "Azure Cache for Redis"
        elif 'cache.amazonaws.com' in self.redis_url:
            return "AWS ElastiCache"
        elif 'localhost' in self.redis_url or '127.0.0.1' in self.redis_url:
            return "Local Redis"
        else:
            return "External Redis"

    async def connect(self) -> bool:
        """Подключается к Redis с оптимизацией для внешних сервисов"""
        if not REDIS_AVAILABLE:
            return False

        try:
            # Настройки подключения в зависимости от сервиса
            if self.service_type == "Upstash":
                # Upstash оптимизации
                self.redis_client = redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_timeout=self.socket_timeout,
                    socket_connect_timeout=self.connection_timeout,
                    retry_on_timeout=self.retry_on_timeout,
                    health_check_interval=30,  # Проверка здоровья каждые 30 сек
                    max_connections=5,  # Ограничиваем соединения для бесплатного тарифа
                )
            elif self.service_type == "Local Redis":
                # Локальный Redis - стандартные настройки
                self.redis_client = redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_timeout=self.socket_timeout,
                    socket_connect_timeout=self.connection_timeout,
                )
            else:
                # Внешние сервисы - консервативные настройки
                self.redis_client = redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_timeout=self.socket_timeout,
                    socket_connect_timeout=self.connection_timeout,
                    retry_on_timeout=self.retry_on_timeout,
                    health_check_interval=60,
                    max_connections=3,
                )

            # Проверяем соединение с таймаутом
            try:
                await asyncio.wait_for(self.redis_client.ping(), timeout=5.0)
            except asyncio.TimeoutError:
                logging.error(f"Redis ping timeout for {self.service_type}")
                return False

            # Проверяем доступность команд для внешних сервисов
            await self._test_redis_capabilities()

            logging.info(f"Connected to {self.service_type} Redis at {self.redis_url}")
            return True

        except Exception as e:
            logging.error(f"Failed to connect to {self.service_type} Redis: {e}")
            return False

    async def _test_redis_capabilities(self):
        """Тестирует доступность Redis команд для внешних сервисов"""
        try:
            # Тестируем основные команды
            test_key = "gemaibot:test:capabilities"
            test_data = {"test": "data", "timestamp": datetime.now().isoformat()}

            # Тест Hash операций
            await self.redis_client.hset(test_key, "data", json.dumps(test_data))
            result = await self.redis_client.hget(test_key, "data")
            if result:
                logging.debug(f"Redis capabilities test passed: Hash operations OK")

            # Тест Sorted Set операций
            await self.redis_client.zadd(f"{test_key}:zset", {"test": 1.0})
            zset_result = await self.redis_client.zcard(f"{test_key}:zset")
            if zset_result == 1:
                logging.debug(f"Redis capabilities test passed: Sorted Set operations OK")

            # Очищаем тестовые данные
            await self.redis_client.delete(test_key, f"{test_key}:zset")

        except Exception as e:
            logging.warning(f"Redis capabilities test failed: {e}")
            # Продолжаем работу, но логируем предупреждение

    async def disconnect(self):
        """Отключается от Redis"""
        if self.redis_client:
            try:
                await self.redis_client.close()
                logging.info(f"Disconnected from {self.service_type} Redis")
            except Exception as e:
                logging.warning(f"Error during Redis disconnect: {e}")

    async def enqueue(self, task: Task) -> bool:
        """Добавляет задачу в очередь с оптимизацией для внешних сервисов"""
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

            # Для Upstash и других внешних сервисов используем pipeline для атомарности
            if self.service_type != "Local Redis":
                async with self.redis_client.pipeline() as pipe:
                    # Сохраняем задачу
                    await pipe.hset(self.tasks_key, task.id, json.dumps(task_data))
                    # Добавляем в очередь с приоритетом
                    score = -task.priority.value
                    await pipe.zadd(self.queue_key, {task.id: score})
                    # Выполняем все операции атомарно
                    await pipe.execute()
            else:
                # Локальный Redis - обычные операции
                await self.redis_client.hset(self.tasks_key, task.id, json.dumps(task_data))
                score = -task.priority.value
                await self.redis_client.zadd(self.queue_key, {task.id: score})

            logging.debug(f"Enqueued task {task.id} to {self.service_type} Redis queue")
            return True

        except Exception as e:
            logging.error(f"Failed to enqueue task {task.id} to {self.service_type}: {e}")
            return False

    async def dequeue(self, timeout: int = 1) -> Optional[Task]:
        """Извлекает задачу из очереди с оптимизацией для внешних сервисов"""
        if not self.redis_client:
            return None

        try:
            # Для внешних сервисов используем неблокирующий pop
            if self.service_type != "Local Redis":
                result = await self.redis_client.zpopmin(self.queue_key, count=1)
                if not result:
                    return None
                task_id, score = result[0]
            else:
                # Локальный Redis - блокирующий pop
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

            logging.debug(f"Dequeued task {task_id} from {self.service_type} Redis queue")
            return task

        except Exception as e:
            logging.error(f"Failed to dequeue task from {self.service_type}: {e}")
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

            logging.debug(f"Completed task {task.id} in {self.service_type}")
            return True

        except Exception as e:
            logging.error(f"Failed to complete task {task.id} in {self.service_type}: {e}")
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
                score = -task.priority.value + task.retry_count
                await self.redis_client.zadd(self.queue_key, {task.id: score})
                logging.debug(f"Retrying task {task.id} in {self.service_type} (attempt {task.retry_count}/{task.max_retries})")
            else:
                # Помечаем как неудачную
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now()
                logging.debug(f"Failed task {task.id} in {self.service_type} after {task.retry_count} attempts")

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
            logging.error(f"Failed to handle task failure {task.id} in {self.service_type}: {e}")
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
            logging.error(f"Failed to get task status {task_id} from {self.service_type}: {e}")
            return None

    async def get_queue_stats(self) -> Dict[str, Any]:
        """Получает статистику очереди с оптимизацией для внешних сервисов"""
        if not self.redis_client:
            return {}

        try:
            # Размер очереди
            queue_size = await self.redis_client.zcard(self.queue_key)

            # Количество обрабатываемых задач
            processing_count = await self.redis_client.hlen(self.processing_key)

            # Общее количество задач
            total_tasks = await self.redis_client.hlen(self.tasks_key)

            # Для внешних сервисов ограничиваем сканирование
            if self.service_type != "Local Redis":
                # Используем более эффективные методы для внешних сервисов
                status_counts = {
                    'pending': queue_size,
                    'running': processing_count,
                    'completed': 0,
                    'failed': 0,
                    'cancelled': 0
                }

                # Ограничиваем сканирование для бесплатных тарифов
                if total_tasks > 100:
                    logging.debug(f"Large task count ({total_tasks}) in {self.service_type}, limiting status scan")
                    # Сканируем только последние 100 задач
                    recent_tasks = await self.redis_client.hscan(self.tasks_key, count=100)
                    for _, task_data_json in recent_tasks[1].items():
                        try:
                            task_data = json.loads(task_data_json)
                            status = task_data.get('status', 'unknown')
                            if status in status_counts:
                                status_counts[status] += 1
                        except:
                            continue
                else:
                    # Для небольших объемов сканируем все
                    all_tasks = await self.redis_client.hgetall(self.tasks_key)
                    for task_data_json in all_tasks.values():
                        try:
                            task_data = json.loads(task_data_json)
                            status = task_data.get('status', 'unknown')
                            if status in status_counts:
                                status_counts[status] += 1
                        except:
                            continue
            else:
                # Локальный Redis - полное сканирование
                status_counts = {status.value: 0 for status in TaskStatus}
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
                'cancelled_tasks': status_counts.get('cancelled', 0),
                'service_type': self.service_type
            }

        except Exception as e:
            logging.error(f"Failed to get queue stats from {self.service_type}: {e}")
            return {}

    async def cleanup_old_tasks(self, older_than_days: int = 7) -> int:
        """Очищает старые завершённые задачи с оптимизацией для внешних сервисов"""
        if not self.redis_client:
            return 0

        try:
            cutoff_date = datetime.now() - timedelta(days=older_than_days)
            cleaned_count = 0

            # Для внешних сервисов используем более эффективную очистку
            if self.service_type != "Local Redis":
                # Ограничиваем количество сканируемых задач
                max_scan = 100
                cursor = 0
                scanned_count = 0

                while cursor != 0 and scanned_count < max_scan:
                    cursor, batch = await self.redis_client.hscan(
                        self.tasks_key,
                        cursor=cursor,
                        count=20
                    )

                    for task_id, task_data_json in batch.items():
                        if scanned_count >= max_scan:
                            break

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

                        scanned_count += 1

                    if scanned_count >= max_scan:
                        logging.info(f"Reached scan limit for {self.service_type}, cleaned {cleaned_count} tasks")
                        break
            else:
                # Локальный Redis - полная очистка
                all_tasks = await self.redis_client.hgetall(self.tasks_key)
                for task_id, task_data_json in all_tasks.items():
                    try:
                        task_data = json.loads(task_data_json)
                        status = task_data.get('status')
                        completed_at_str = task_data.get('completed_at')

                        if status in ['completed', 'failed', 'cancelled'] and completed_at_str:
                            completed_at = datetime.fromisoformat(completed_at_str)
                            if completed_at < cutoff_date:
                                await self.redis_client.hdel(self.tasks_key, task_id)
                                cleaned_count += 1

                    except Exception:
                        continue

            if cleaned_count > 0:
                logging.info(f"Cleaned up {cleaned_count} old tasks from {self.service_type} Redis queue")
            return cleaned_count

        except Exception as e:
            logging.error(f"Failed to cleanup old tasks from {self.service_type}: {e}")
            return 0


# Глобальный экземпляр Redis очереди
redis_queue = RedisQueue() if REDIS_AVAILABLE else None


async def init_redis_queue() -> bool:
    """Инициализирует Redis очередь если доступна"""
    if not redis_queue:
        return False

    if await redis_queue.connect():
        logging.info(f"Redis queue initialized successfully for {redis_queue.service_type}")
        return True
    else:
        logging.warning(f"Redis queue initialization failed for {redis_queue.service_type}")
        return False


async def cleanup_redis_queue():
    """Очищает Redis очередь"""
    if redis_queue:
        await redis_queue.disconnect()


# Функции для мониторинга и диагностики
async def get_redis_service_info() -> Dict[str, Any]:
    """Получает информацию о Redis сервисе"""
    if not redis_queue or not redis_queue.redis_client:
        return {"status": "not_available"}

    try:
        # Проверяем соединение с таймаутом
        await asyncio.wait_for(redis_queue.redis_client.ping(), timeout=3.0)

        info = await redis_queue.redis_client.info()
        return {
            "service_type": redis_queue.service_type,
            "redis_version": info.get("redis_version", "unknown"),
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "connected_clients": info.get("connected_clients", 0),
            "total_commands_processed": info.get("total_commands_processed", 0),
            "keyspace_hits": info.get("keyspace_hits", 0),
            "keyspace_misses": info.get("keyspace_misses", 0),
            "uptime_in_seconds": info.get("uptime_in_seconds", 0)
        }
    except asyncio.TimeoutError:
        return {
            "service_type": redis_queue.service_type,
            "status": "timeout",
            "error": "Connection timeout - Redis server not responding"
        }
    except Exception as e:
        return {
            "service_type": redis_queue.service_type,
            "status": "error",
            "error": str(e)
        }
