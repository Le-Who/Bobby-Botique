"""A saturated fallback queue must expose a failed retry, never a phantom pending job."""

import asyncio

from app.queue import TaskQueue, TaskStatus


async def test_retry_capacity_failure_is_terminal_and_not_reported_scheduled(monkeypatch):
    from app import queue as module

    queue = TaskQueue(max_workers=1)
    queue._fallback_queue = asyncio.PriorityQueue(maxsize=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    captured = []
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.01 if timeout == 2.0 else timeout)

    async def execute(task):
        if task.data.get("fail"):
            entered.set()
            await release.wait()
            raise ValueError("synthetic execution failure")
        return {"status": "completed"}

    def emit(event, **fields):
        captured.append((event, fields))
        if event == "job.finished" and fields.get("job_id") == failed_id:
            finished.set()

    monkeypatch.setattr(module, "_get_redis", lambda: None)
    monkeypatch.setattr(module.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(queue, "_execute_task", execute)
    monkeypatch.setattr(module, "emit", emit)
    failed_id = await queue.add_task(user_id=1, task_type="test", data={"fail": True})
    await queue.start()
    try:
        await entered.wait()
        queued_id = await queue.add_task(user_id=2, task_type="test", data={})
        assert queued_id
        release.set()
        await finished.wait()
        task = queue.tasks[failed_id]
        assert task.status is TaskStatus.FAILED
        assert task.completed_at is not None
        assert not [
            fields for event, fields in captured if event == "job.retry_scheduled" and fields.get("job_id") == failed_id
        ]
        terminal = [fields for event, fields in captured if event == "job.finished" and fields["job_id"] == failed_id]
        assert len(terminal) == 1
        assert terminal[0]["retry_disposition"] == "capacity_rejected"
        assert any(event == "job.capacity_rejected" for event, _ in captured)
    finally:
        release.set()
        await queue.stop()
