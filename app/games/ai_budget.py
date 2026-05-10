from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal

from app.cache import redis_client

logger = logging.getLogger(__name__)

HintGenerationMode = Literal["foreground", "background"]

_AI_STUDIO_FOREGROUND_RPM: dict[str, int] = {
    "gemini-3-flash-preview": 4,
    "gemini-3.1-flash-lite": 14,
    "gemini-2.5-flash-lite": 9,
    "gemini-2.5-flash": 2,
}
_AI_STUDIO_BACKGROUND_RPM: dict[str, int] = {
    "gemini-3.1-flash-lite": 2,
}
_LOCAL_CONCURRENCY_LIMITS: dict[tuple[str, str], int] = {
    ("foreground", "ai_studio"): 3,
    ("background", "ai_studio"): 1,
    ("foreground", "vertex_express"): 6,
    ("background", "vertex_express"): 1,
    ("foreground", "opencode_go"): 4,
    ("background", "opencode_go"): 1,
    ("foreground", "openrouter"): 4,
    ("background", "openrouter"): 1,
}


@dataclass(frozen=True)
class GeminiCooldownState:
    provider: str
    model: str
    cooldown_until: float
    reason: str
    last_retry_after_seconds: int | None = None


class BudgetLease:
    def __init__(self, manager: _BudgetManager, *, scope: str, provider: str, model: str, semaphore) -> None:
        self._manager = manager
        self._scope = scope
        self._provider = provider
        self._model = model
        self._semaphore = semaphore
        self._released = False

    async def __aenter__(self) -> BudgetLease:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()
        self._manager.release(self._scope)


class _BudgetManager:
    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._cooldowns: dict[tuple[str, str], GeminiCooldownState] = {}
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._foreground_active = 0

    async def acquire(self, scope: str, use_case: str, provider: str, model: str) -> BudgetLease | None:
        provider = _normalize_provider(provider)
        if scope == "background" and self.should_pause_background_prefetch():
            return None
        if provider == "ai_studio" and self.is_model_cooldown_active(provider, model):
            return None

        limit = _rpm_limit(scope, provider, model)
        if limit == 0:
            return None
        if limit is not None and not await self._try_consume_window(scope, use_case, provider, model, limit):
            return None

        sem = self._semaphore(scope, provider)
        if scope == "background":
            if sem.locked():
                return None
            await sem.acquire()
        else:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=0.25)
            except TimeoutError:
                return None

        if scope == "foreground":
            self._foreground_active += 1
        return BudgetLease(self, scope=scope, provider=provider, model=model, semaphore=sem)

    def release(self, scope: str) -> None:
        if scope == "foreground" and self._foreground_active > 0:
            self._foreground_active -= 1

    async def record_result(
        self,
        provider: str,
        model: str,
        outcome: str,
        *,
        retry_after_seconds: int | None = None,
        reason: str = "",
    ) -> None:
        provider = _normalize_provider(provider)
        if provider != "ai_studio":
            return

        if outcome == "success":
            self._cooldowns.pop((provider, model), None)
            self._prune_expired_cooldowns()
            return

        if outcome != "rate_limit":
            return

        wait_s = max(15, min(retry_after_seconds or 60, 300))
        cooldown_until = time.monotonic() + wait_s
        key = (provider, model)
        current = self._cooldowns.get(key)
        if current and current.cooldown_until >= cooldown_until:
            return
        self._cooldowns[key] = GeminiCooldownState(
            provider=provider,
            model=model,
            cooldown_until=cooldown_until,
            reason=(reason or "rate_limit")[:500],
            last_retry_after_seconds=wait_s,
        )
        logger.warning(
            "Crocodile AI budget cooldown set provider=%s model=%s retry_after=%ss reason=%s",
            provider,
            model,
            wait_s,
            (reason or "rate_limit")[:200],
        )

    def get_cooldown(self, provider: str, model: str) -> GeminiCooldownState | None:
        provider = _normalize_provider(provider)
        self._prune_expired_cooldowns()
        cooldown = self._cooldowns.get((provider, model))
        if cooldown and cooldown.cooldown_until > time.monotonic():
            return cooldown
        return None

    def is_model_cooldown_active(self, provider: str, model: str) -> bool:
        return self.get_cooldown(provider, model) is not None

    def has_any_ai_studio_cooldown(self) -> bool:
        self._prune_expired_cooldowns()
        now = time.monotonic()
        return any(state.provider == "ai_studio" and state.cooldown_until > now for state in self._cooldowns.values())

    def should_pause_background_prefetch(self) -> bool:
        return self._foreground_active > 0 or self.has_any_ai_studio_cooldown()

    def reset(self) -> None:
        self._windows.clear()
        self._cooldowns.clear()
        self._semaphores.clear()
        self._foreground_active = 0

    async def _try_consume_window(self, scope: str, use_case: str, provider: str, model: str, limit: int) -> bool:
        now = time.time()
        window_key = (provider, model)
        local_window = self._windows[window_key]
        cutoff = now - 60.0
        while local_window and local_window[0] <= cutoff:
            local_window.popleft()
        if len(local_window) >= limit:
            return False

        if redis_client:
            redis_key = f"croc:ai_budget:{scope}:{provider}:{model}"
            try:
                await redis_client.zremrangebyscore(redis_key, 0, cutoff)  # type: ignore[misc]
                current = await redis_client.zcard(redis_key)  # type: ignore[misc]
                if int(current) >= limit:
                    return False
                member = f"{use_case}:{uuid.uuid4()}"
                await redis_client.zadd(redis_key, {member: now})  # type: ignore[misc]
                await redis_client.expire(redis_key, 90)  # type: ignore[misc]
            except Exception as exc:
                logger.debug("Crocodile AI budget Redis window fallback for %s/%s: %s", provider, model, exc)

        local_window.append(now)
        return True

    def _prune_expired_cooldowns(self) -> None:
        now = time.monotonic()
        expired = [key for key, state in self._cooldowns.items() if state.cooldown_until <= now]
        for key in expired:
            self._cooldowns.pop(key, None)

    def _semaphore(self, scope: str, provider: str) -> asyncio.Semaphore:
        key = (scope, provider)
        sem = self._semaphores.get(key)
        if sem is None:
            limit = _LOCAL_CONCURRENCY_LIMITS.get(key, 1)
            sem = asyncio.Semaphore(limit)
            self._semaphores[key] = sem
        return sem


def _normalize_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized in {"vertex", "vertex_ai", "vertex_express"}:
        return "vertex_express"
    if normalized in {"opencode", "opencode_go"}:
        return "opencode_go"
    if normalized in {"openrouter"}:
        return "openrouter"
    return "ai_studio"


def _rpm_limit(scope: str, provider: str, model: str) -> int | None:
    if provider != "ai_studio":
        return None
    if scope == "background":
        return _AI_STUDIO_BACKGROUND_RPM.get(model, 0)
    return _AI_STUDIO_FOREGROUND_RPM.get(model, 1)


_MANAGER = _BudgetManager()


async def acquire_foreground_slot(use_case: str, provider: str, model: str) -> BudgetLease | None:
    return await _MANAGER.acquire("foreground", use_case, provider, model)


async def acquire_background_slot(use_case: str, provider: str, model: str) -> BudgetLease | None:
    return await _MANAGER.acquire("background", use_case, provider, model)


async def record_result(
    provider: str,
    model: str,
    outcome: str,
    retry_after_seconds: int | None = None,
    reason: str = "",
) -> None:
    await _MANAGER.record_result(
        provider,
        model,
        outcome,
        retry_after_seconds=retry_after_seconds,
        reason=reason,
    )


def is_model_cooldown_active(provider: str, model: str) -> bool:
    return _MANAGER.is_model_cooldown_active(provider, model)


def get_model_cooldown(provider: str, model: str) -> GeminiCooldownState | None:
    return _MANAGER.get_cooldown(provider, model)


def has_any_ai_studio_cooldown() -> bool:
    return _MANAGER.has_any_ai_studio_cooldown()


def should_pause_background_prefetch() -> bool:
    return _MANAGER.should_pause_background_prefetch()


def reset_budget_state_for_tests() -> None:
    _MANAGER.reset()
