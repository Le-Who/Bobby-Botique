"""Public queue facade; v2 workers never execute legacy list entries."""

from app.queue_legacy import Task as Task
from app.queue_legacy import TaskPriority as TaskPriority
from app.queue_legacy import TaskStatus as TaskStatus
from app.queue_runtime import TaskQueue as TaskQueue
from app.queue_runtime import current_job_guard as current_job_guard


def _get_redis():
    from app.cache import redis_client

    return redis_client


def __getattr__(name):
    # Wire-format helpers and Lua v1 constants remain available to controlled
    # migration tools and compatibility tests, not to the v2 worker loop.
    from app import queue_legacy

    if name.startswith("_") and hasattr(queue_legacy, name):
        return getattr(queue_legacy, name)
    raise AttributeError(name)


task_queue = TaskQueue()


async def start_task_queue():
    await task_queue.start()


async def stop_task_queue():
    await task_queue.stop()


async def add_background_task(user_id, task_type, data, priority=TaskPriority.NORMAL):
    return await task_queue.add_task(user_id, task_type, data, priority)


async def enqueue_job(*args, **kwargs):
    return await task_queue.enqueue_job(*args, **kwargs)


async def read_job(task_id):
    return await task_queue.read_job(task_id)


async def read_job_by_operation(user_id, idempotency_key):
    return await task_queue.read_job_by_operation(user_id, idempotency_key)


async def read_jobs_by_conflicts(user_id, lane, conflict_keys):
    return await task_queue.read_jobs_by_conflicts(user_id, lane, conflict_keys)


async def get_task_status(task_id):
    return await task_queue.get_task_status(task_id)


async def cancel_user_task(task_id, user_id):
    return await task_queue.cancel_task(task_id, user_id)
