"""Single-flight cache for shared administrative telemetry, never personal data."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any


class SourceUnavailable(RuntimeError):
    """A telemetry reader returned an explicit unavailable result, not a value."""


@dataclass(frozen=True)
class PartialSourceValue:
    """A fresh bounded observation whose unavailable sub-sources remain explicit."""

    value: Any


@dataclass(frozen=True)
class SourceSnapshot:
    value: Any
    observed_at: float | None
    refresh_started_at: float | None
    available: bool
    stale: bool
    reason_code: str | None
    scope: str = "process"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Cached:
    snapshot: SourceSnapshot
    observed_clock: float
    attempted_clock: float


class DashboardSourceCache:
    """One owner task per registered source; reader cancellation cannot kill it.

    Every instance is confined to one event loop and one authorization domain.
    Only named, shared admin sources can be registered; callers cannot create
    cache keys from user IDs, session tokens or arbitrary source arguments.
    """

    def __init__(
        self,
        sources: Mapping[str, Callable[[], Awaitable[Any]]],
        *,
        timeout_seconds: float = 5,
        stale_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        scope: str = "process",
        source_scopes: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or stale_seconds < 0:
            raise ValueError("Dashboard source time budgets must be positive")
        self._sources = dict(sources)
        self._timeout = timeout_seconds
        self._stale_seconds = stale_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._scope = scope
        self._source_scopes = dict(source_scopes or {})
        self._cache: dict[str, _Cached] = {}
        self._good: dict[str, _Cached] = {}
        self._inflight: dict[str, asyncio.Task[SourceSnapshot]] = {}
        self._versions = dict.fromkeys(sources, 0)
        self._closed = False

    def invalidate(self, name: str) -> None:
        if name not in self._sources:
            raise KeyError(name)
        self._versions[name] += 1
        self._cache.pop(name, None)

    def _copy_current(self, entry: _Cached) -> SourceSnapshot:
        result = entry.snapshot
        if result.stale and self._clock() - entry.observed_clock > self._stale_seconds:
            result = replace(result, value=None, available=False)
        return copy.deepcopy(result)

    async def get(self, name: str, *, max_age_seconds: float) -> SourceSnapshot:
        if name not in self._sources:
            raise KeyError(name)
        if max_age_seconds < 0:
            raise ValueError("Source max age cannot be negative")
        if self._closed:
            return SourceSnapshot(
                None, None, None, False, False, "cache_closed", self._source_scopes.get(name, self._scope)
            )
        # A save during an in-flight read invalidates that read. One joined retry
        # reconciles a burst without spawning a refresh for each mutation/reader.
        for _ in range(2):
            entry = self._cache.get(name)
            if entry is not None and self._clock() - entry.attempted_clock < max_age_seconds:
                return self._copy_current(entry)
            task = self._inflight.get(name)
            if task is None:
                task = asyncio.create_task(self._refresh(name, self._versions[name]), name=f"dashboard:{name}")
                self._inflight[name] = task
            snapshot = await asyncio.shield(task)
            if snapshot.reason_code != "source_invalidated":
                return copy.deepcopy(snapshot)
        return copy.deepcopy(snapshot)

    async def _refresh(self, name: str, version: int) -> SourceSnapshot:
        started_at = self._wall_clock()
        started_clock = self._clock()
        scope = self._source_scopes.get(name, self._scope)
        try:
            try:
                async with asyncio.timeout(self._timeout):
                    value = await self._sources[name]()
                partial = isinstance(value, PartialSourceValue)
                if partial:
                    value = value.value
                snapshot = SourceSnapshot(
                    copy.deepcopy(value),
                    self._wall_clock(),
                    started_at,
                    True,
                    False,
                    "source_partial" if partial else None,
                    scope,
                )
                observed_clock = self._clock()
                if version != self._versions[name]:
                    return replace(snapshot, stale=True, reason_code="source_invalidated")
                self._good[name] = _Cached(snapshot, observed_clock, started_clock)
            except Exception as error:
                reason = (
                    "source_timeout"
                    if isinstance(error, TimeoutError)
                    else "source_unavailable"
                    if isinstance(error, SourceUnavailable)
                    else "source_failed"
                )
                good = self._good.get(name)
                observed_clock = good.observed_clock if good is not None else started_clock
                if good is not None and self._clock() - good.observed_clock <= self._stale_seconds:
                    snapshot = replace(good.snapshot, refresh_started_at=started_at, stale=True, reason_code=reason)
                else:
                    snapshot = SourceSnapshot(
                        None,
                        good.snapshot.observed_at if good else None,
                        started_at,
                        False,
                        good is not None,
                        reason,
                        scope,
                    )
                if version != self._versions[name]:
                    return replace(snapshot, stale=True, reason_code="source_invalidated")
            self._cache[name] = _Cached(snapshot, observed_clock, started_clock)
            return snapshot
        finally:
            if self._inflight.get(name) is asyncio.current_task():
                self._inflight.pop(name, None)

    async def close(self) -> None:
        self._closed = True
        owned = tuple(self._inflight.values())
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        self._good.clear()


class BotLifecycle:
    """Supervisor state is independent of a listening HTTP server."""

    STATES = frozenset({"starting", "ready", "draining", "stopped", "failed"})

    def __init__(self) -> None:
        self.state = "stopped"
        self.changed_at = time.time()

    def set(self, state: str) -> None:
        if state not in self.STATES:
            raise ValueError("Unsupported bot lifecycle state")
        if self.state == "failed" and state != "starting":
            return  # Cleanup cannot erase evidence of a failed supervisor.
        self.state = state
        self.changed_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ready": self.state == "ready",
            "changed_at": self.changed_at,
            "observed_at": time.time(),
            "scope": "process",
            "available": True,
        }


bot_lifecycle = BotLifecycle()
