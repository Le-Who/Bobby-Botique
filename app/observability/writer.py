"""Bounded, non-blocking log transport to a dedicated writer thread."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from typing import Protocol


class TextStream(Protocol):
    def write(self, value: str) -> int: ...

    def flush(self) -> None: ...


def _level_name(levelno: int) -> str:
    return logging.getLevelName(levelno).lower()


class BoundedLogWriter:
    """Accept serialized events without waiting for potentially slow stream I/O."""

    def __init__(
        self,
        stream: TextStream,
        *,
        max_events: int = 4096,
        max_bytes: int = 8 * 1024 * 1024,
        auto_start: bool = True,
        loss_summary_factory: Callable[[Mapping[str, object]], bytes] | None = None,
        loss_summary_interval: float = 30.0,
    ) -> None:
        if max_events <= 0 or max_bytes <= 0 or loss_summary_interval <= 0:
            raise ValueError("Log queue limits must be positive")
        self._stream = stream
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._low_event_limit = max(1, max_events - max(1, max_events // 4))
        self._low_byte_limit = max(1, max_bytes - max(1, max_bytes // 4))
        self._queue: deque[tuple[bytes, int]] = deque()
        self._queued_bytes = 0
        self._drops: Counter[str] = Counter()
        self._drop_reasons: Counter[str] = Counter()
        self._pending_drops: Counter[str] = Counter()
        self._pending_drop_reasons: Counter[str] = Counter()
        self._loss_summary_factory = loss_summary_factory
        self._loss_summary_interval = loss_summary_interval
        self._loss_window_started: float | None = None
        self._loss_summary_deadline: float | None = None
        self._write_failures = 0
        self._accepting = True
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        if auto_start:
            self.start()

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            if not self._accepting:
                raise RuntimeError("Cannot restart a stopped log writer")
            self._thread = threading.Thread(
                target=self._run,
                name="gemaibot-log-writer",
                daemon=True,
            )
            self._thread.start()

    def submit(self, payload: bytes, *, levelno: int) -> bool:
        """Enqueue one line or account for its loss without blocking on stream I/O."""
        if not isinstance(payload, bytes):
            raise TypeError("Log payload must be serialized bytes")
        payload_size = len(payload)
        high_priority = levelno >= logging.WARNING
        with self._condition:
            if not self._accepting:
                self._record_drop_locked(levelno, "writer_stopped")
                return False
            event_limit = self._max_events if high_priority else self._low_event_limit
            byte_limit = self._max_bytes if high_priority else self._low_byte_limit
            if high_priority and payload_size <= self._max_bytes:
                while len(self._queue) >= self._max_events or self._queued_bytes + payload_size > self._max_bytes:
                    low_index = next(
                        (
                            index
                            for index, (_queued, queued_level) in enumerate(self._queue)
                            if queued_level < logging.WARNING
                        ),
                        None,
                    )
                    if low_index is None:
                        break
                    evicted_payload, evicted_level = self._queue[low_index]
                    del self._queue[low_index]
                    self._queued_bytes -= len(evicted_payload)
                    self._record_drop_locked(evicted_level, "evicted_for_priority")
            reason: str | None = None
            if payload_size > self._max_bytes:
                reason = "payload_too_large"
            elif len(self._queue) >= event_limit:
                reason = "event_capacity"
            elif self._queued_bytes + payload_size > byte_limit:
                reason = "byte_capacity"
            if reason is not None:
                self._record_drop_locked(levelno, reason)
                return False
            self._queue.append((payload, levelno))
            self._queued_bytes += payload_size
            self._condition.notify()
            return True

    @property
    def queued_events(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def queued_bytes(self) -> int:
        with self._condition:
            return self._queued_bytes

    @property
    def dropped_by_level(self) -> Mapping[str, int]:
        with self._condition:
            return dict(self._drops)

    @property
    def dropped_by_reason(self) -> Mapping[str, int]:
        with self._condition:
            return dict(self._drop_reasons)

    @property
    def write_failures(self) -> int:
        with self._condition:
            return self._write_failures

    def stop(self, *, timeout: float = 3.0) -> bool:
        """Stop accepting new events and drain accepted events within ``timeout``."""
        with self._condition:
            if self._thread is None and (
                self._queue or (self._pending_drops and self._loss_summary_factory is not None)
            ):
                self.start()
            self._accepting = False
            self._condition.notify_all()
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def _record_drop_locked(self, levelno: int, reason: str) -> None:
        level = _level_name(levelno)
        self._drops[level] += 1
        self._drop_reasons[reason] += 1
        self._pending_drops[level] += 1
        self._pending_drop_reasons[reason] += 1
        if self._loss_window_started is None:
            self._loss_window_started = time.time()
            self._loss_summary_deadline = time.monotonic() + self._loss_summary_interval
        try:
            from app.observability.metrics import operational_metrics

            operational_metrics.observe(
                "logging.loss_summary",
                {"dropped_level": level, "reason_code": reason, "dropped_count": 1},
            )
        except Exception:
            pass
        self._condition.notify()

    def _take_loss_summary_locked(self, *, force: bool) -> Mapping[str, object] | None:
        if not self._pending_drops or self._loss_summary_factory is None:
            return None
        if not force and self._loss_summary_deadline is not None and time.monotonic() < self._loss_summary_deadline:
            return None
        snapshot: dict[str, object] = {
            "dropped_by_level": dict(self._pending_drops),
            "dropped_by_reason": dict(self._pending_drop_reasons),
            "window_started_epoch": self._loss_window_started,
            "window_ended_epoch": time.time(),
            "queued_events": len(self._queue),
            "queued_bytes": self._queued_bytes,
        }
        self._pending_drops.clear()
        self._pending_drop_reasons.clear()
        self._loss_window_started = None
        self._loss_summary_deadline = None
        return snapshot

    def _write_payload(self, payload: bytes) -> None:
        try:
            self._stream.write(payload.decode("utf-8") + "\n")
            self._stream.flush()
        except Exception:
            with self._condition:
                self._write_failures += 1

    def _run(self) -> None:
        while True:
            payload: bytes | None = None
            summary: Mapping[str, object] | None = None
            with self._condition:
                while payload is None and summary is None:
                    if self._queue:
                        payload, _levelno = self._queue.popleft()
                        self._queued_bytes -= len(payload)
                        self._condition.notify_all()
                        break

                    summary = self._take_loss_summary_locked(force=not self._accepting)
                    if summary is not None:
                        break
                    if not self._accepting:
                        return

                    wait_timeout = None
                    if self._loss_summary_deadline is not None:
                        wait_timeout = max(0.0, self._loss_summary_deadline - time.monotonic())
                    self._condition.wait(timeout=wait_timeout)

            if summary is not None:
                assert self._loss_summary_factory is not None
                try:
                    payload = self._loss_summary_factory(summary)
                except Exception:
                    with self._condition:
                        self._write_failures += 1
                    continue
            assert payload is not None
            self._write_payload(payload)


class BoundedQueueHandler(logging.Handler):
    """Format on the producer thread, then enqueue UTF-8 bytes for output."""

    _gemaibot_owned = True

    def __init__(self, writer: BoundedLogWriter, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = self.format(record).encode("utf-8")
            self.writer.submit(payload, levelno=record.levelno)
        except Exception:
            self.handleError(record)
