"""Request-wide admission must survive fallback and distinguish unknown usage."""

import pytest

from app.core.research_budget import BudgetExhausted, ResearchBudget


def test_fallback_inherits_page_token_and_attempt_limits():
    budget = ResearchBudget(timeout_seconds=90, max_tokens=100, max_pages=1, max_attempts=2)
    budget.begin_attempt()
    assert budget.admit_page()
    budget.record_usage(100)
    with pytest.raises(BudgetExhausted, match="tokens"):
        budget.begin_attempt()
    assert not budget.admit_page()
    assert budget.tokens_known == 100


def test_reserve_and_deadline_use_one_clock():
    now = [100.0]
    budget = ResearchBudget(timeout_seconds=90, clock=lambda: now[0])
    budget.begin_attempt()
    now[0] = 175
    assert budget.remaining_seconds() == 15
    with pytest.raises(BudgetExhausted, match="deadline"):
        budget.check_iteration()
    assert budget.remaining_seconds(synthesis=True) == 15
    now[0] = 190
    assert budget.remaining_seconds(synthesis=True) == 0


def test_missing_usage_reserves_tokens_but_zero_is_known():
    budget = ResearchBudget(max_tokens=4000)
    budget.record_usage(0)
    assert not budget.usage_uncertain
    budget.record_usage(None)
    assert budget.usage_uncertain
    assert budget.tokens_known == 0
    assert budget.tokens_reserved == 4000
    with pytest.raises(BudgetExhausted, match="tokens"):
        budget.check_iteration()


def test_attempt_cap_is_shared():
    budget = ResearchBudget(max_attempts=1)
    budget.begin_attempt()
    with pytest.raises(BudgetExhausted, match="attempts"):
        budget.begin_attempt()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"max_pages": -1},
        {"max_tokens": 0},
        {"max_attempts": 0},
    ],
)
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        ResearchBudget(**kwargs)
