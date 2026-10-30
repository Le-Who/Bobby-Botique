"""Broadcast batches have their own bounded worker lane."""

import pytest

from app.queue import TaskQueue
from app.queue_contracts import AdmissionState
from app.queue_store import MemoryQueueStore


@pytest.mark.asyncio
async def test_broadcast_admission_is_capped_at_ten_without_spending_daily_slots():
    queue = TaskQueue(store=MemoryQueueStore())
    for i in range(10):
        result = await queue.enqueue_job(
            0, "broadcast_delivery", {"batch_id": str(i)}, idempotency_key=f"b:{i}", lane="broadcast"
        )
        assert result.state is AdmissionState.ACCEPTED
    rejected = await queue.enqueue_job(0, "broadcast_delivery", {}, idempotency_key="b:overflow", lane="broadcast")
    assert rejected.state is AdmissionState.REJECTED
    assert (await queue.read_lane("broadcast")).workers == 1
    assert (await queue.read_lane("daily")).workers == 2
    stats = await queue.get_queue_stats()
    assert stats["lanes"]["broadcast"]["nonterminal"] == 10
    assert stats["lanes"]["daily"]["nonterminal"] == 0
