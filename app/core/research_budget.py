"""Request-owned research admission; independent of provider/delivery outcomes.

Time bounds cancel cooperative async work. Token usage is a soft admission cap:
an already admitted call can exceed it, and one final synthesis is still allowed.
Unknown usage reserves 4,000 tokens for admission, never for billing.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class BudgetExhausted(RuntimeError):
    """No more research work may be admitted for this reason."""


@dataclass
class ResearchBudget:
    timeout_seconds: float = 90
    max_tokens: int = 100_000
    max_pages: int = 3
    max_attempts: int = 3
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    tokens_known: int = field(default=0, init=False)
    tokens_reserved: int = field(default=0, init=False)
    unknown_usage_calls: int = field(default=0, init=False)
    pages_used: int = field(default=0, init=False)
    attempts_used: int = field(default=0, init=False)
    reason: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        for name in ("max_tokens", "max_pages", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "max_pages" else 1):
                raise ValueError(f"invalid {name}")
        self.started_at = self.clock()

    @property
    def usage_uncertain(self) -> bool:
        return self.unknown_usage_calls > 0

    def remaining_seconds(self, *, synthesis: bool = True) -> float:
        reserve = 0.0 if synthesis else min(15.0, self.timeout_seconds / 6)
        return max(0.0, self.timeout_seconds - max(0.0, self.clock() - self.started_at) - reserve)

    def check_iteration(self) -> None:
        reason = None
        if self.remaining_seconds(synthesis=False) <= 0:
            reason = "deadline"
        elif self.tokens_known + self.tokens_reserved >= self.max_tokens:
            reason = "tokens"
        if reason:
            self.reason = reason
            raise BudgetExhausted(reason)

    def begin_attempt(self) -> None:
        self.check_iteration()
        if self.attempts_used >= self.max_attempts:
            self.reason = "attempts"
            raise BudgetExhausted("attempts")
        self.attempts_used += 1

    def admit_page(self) -> bool:
        if self.pages_used >= self.max_pages:
            return False
        self.pages_used += 1
        return True

    def record_usage(self, tokens: int | None) -> None:
        if tokens is None:
            self.unknown_usage_calls += 1
            self.tokens_reserved += 4000
        elif type(tokens) is not int or tokens < 0:
            raise ValueError("usage must be a nonnegative integer or None")
        else:
            self.tokens_known += tokens
