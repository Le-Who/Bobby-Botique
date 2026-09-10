from __future__ import annotations

import asyncio
import io
import logging
import threading
import time

import pytest

from app.observability.context import current_context, request_scope
from app.observability.writer import BoundedLogWriter, BoundedQueueHandler


class _BlockingStream:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.rows: list[str] = []

    def write(self, value: str) -> int:
        self.entered.set()
        self.release.wait(timeout=5)
        self.rows.append(value)
        return len(value)

    def flush(self) -> None:
        return


def test_low_priority_events_cannot_consume_reserved_capacity():
    """INFO floods must leave count capacity for WARNING+ evidence."""
    writer = BoundedLogWriter(io.StringIO(), max_events=4, max_bytes=1024, auto_start=False)

    assert writer.submit(b"one", levelno=20)
    assert writer.submit(b"two", levelno=20)
    assert writer.submit(b"three", levelno=20)
    assert not writer.submit(b"four", levelno=20)
    assert writer.submit(b"warning", levelno=30)
    assert writer.queued_events == 4
    assert writer.dropped_by_level["info"] == 1


def test_writer_enforces_byte_limit_without_growing_unbounded():
    writer = BoundedLogWriter(io.StringIO(), max_events=10, max_bytes=12, auto_start=False)

    assert writer.submit(b"12345678", levelno=30)
    assert not writer.submit(b"abcdefgh", levelno=30)
    assert writer.queued_bytes == 8
    assert writer.queued_events == 1
    assert writer.dropped_by_reason == {"byte_capacity": 1}


@pytest.mark.asyncio
async def test_blocked_sink_does_not_block_event_loop_producers():
    stream = _BlockingStream()
    writer = BoundedLogWriter(stream, max_events=8, max_bytes=1024)
    try:
        assert writer.submit(b"first", levelno=20)
        assert await asyncio.to_thread(stream.entered.wait, 1.0)

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0)
                ticks += 1

        started = time.perf_counter()
        for index in range(20):
            writer.submit(f"event-{index}".encode(), levelno=20)
        await heartbeat()

        assert ticks == 5
        assert time.perf_counter() - started < 0.25
        assert writer.queued_events <= 8
        assert writer.queued_bytes <= 1024
    finally:
        stream.release.set()
        writer.stop(timeout=1.0)


def test_stop_drains_accepted_lines_in_order():
    stream = io.StringIO()
    writer = BoundedLogWriter(stream, max_events=8, max_bytes=1024)
    assert writer.submit("первая".encode(), levelno=20)
    assert writer.submit(b"second", levelno=30)

    assert writer.stop(timeout=1.0)

    assert stream.getvalue().splitlines() == ["первая", "second"]
    assert writer.queued_events == 0


def test_queue_handler_snapshots_context_before_scope_reset():
    """Moving I/O to a thread must not move ContextVar lookup to that thread."""

    class _ContextFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return f"{current_context().request_id}:{record.getMessage()}"

    stream = io.StringIO()
    writer = BoundedLogWriter(stream, max_events=8, max_bytes=1024, auto_start=False)
    handler = BoundedQueueHandler(writer)
    handler.setFormatter(_ContextFormatter())
    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "inside", (), None)

    with request_scope(request_id="c" * 32):
        handler.handle(record)

    assert writer.stop(timeout=1.0)
    assert stream.getvalue().strip() == f"{'c' * 32}:inside"


def test_drop_summary_is_written_once_during_shutdown_without_recursive_enqueue():
    stream = io.StringIO()
    snapshots: list[dict[str, object]] = []

    def summary_factory(snapshot):
        snapshots.append(dict(snapshot))
        return b"loss-summary"

    writer = BoundedLogWriter(
        stream,
        max_events=2,
        max_bytes=64,
        auto_start=False,
        loss_summary_factory=summary_factory,
    )
    assert writer.submit(b"accepted", levelno=logging.INFO)
    assert not writer.submit(b"dropped", levelno=logging.INFO)

    assert writer.stop(timeout=1.0)
    assert stream.getvalue().splitlines() == ["accepted", "loss-summary"]
    assert len(snapshots) == 1
    assert snapshots[0]["dropped_by_level"] == {"info": 1}
    assert snapshots[0]["dropped_by_reason"] == {"event_capacity": 1}


def test_submit_after_stop_reports_writer_stopped_reason():
    writer = BoundedLogWriter(io.StringIO(), auto_start=False)
    assert writer.stop()

    assert not writer.submit(b"late", levelno=logging.ERROR)
    assert writer.dropped_by_reason == {"writer_stopped": 1}


def test_warning_evicts_oldest_low_priority_event_when_total_queue_is_full():
    writer = BoundedLogWriter(io.StringIO(), max_events=4, max_bytes=1024, auto_start=False)
    assert writer.submit(b"info-1", levelno=logging.INFO)
    assert writer.submit(b"info-2", levelno=logging.INFO)
    assert writer.submit(b"info-3", levelno=logging.INFO)
    assert writer.submit(b"warning-1", levelno=logging.WARNING)

    assert writer.submit(b"critical", levelno=logging.CRITICAL)
    assert writer.queued_events == 4
    assert writer.dropped_by_reason == {"evicted_for_priority": 1}
