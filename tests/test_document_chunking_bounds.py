"""Bounded document retrieval must terminate and use its remaining context."""

import pytest

from app.documents.chunking import chunk_for_context, recursive_chunk


@pytest.mark.parametrize("budget", [0, -1])
def test_recursive_chunk_rejects_nonprogressing_budget_even_for_empty_text(budget):
    with pytest.raises(ValueError, match="max_tokens"):
        recursive_chunk("", max_tokens=budget)


@pytest.mark.parametrize("overlap", [-0.1, 1.0, 1.2, float("nan"), float("inf")])
def test_recursive_chunk_rejects_overlap_that_prevents_progress(overlap):
    with pytest.raises(ValueError, match="overlap_ratio"):
        recursive_chunk("", overlap_ratio=overlap)


def test_context_considers_smaller_relevant_chunks_after_oversized_candidate():
    first = "needle " + "A" * 2990
    second = "secondary " + "B" * 2990
    last = "tailmatch " + "C" * 500
    text = "\n\n".join((first, second, last))
    result = chunk_for_context(text, query="needle secondary tailmatch", max_context_tokens=1143)
    assert result.startswith("needle ")
    assert "tailmatch" in result
    assert len(result) <= 4000


def test_short_document_cannot_exceed_budget_by_token_estimate_rounding():
    text = "x" * 352
    assert len(chunk_for_context(text, max_context_tokens=100)) <= 350


def test_small_budget_retains_top_match_before_unrelated_short_tail():
    text = "needle " + "A" * 3540 + "\n\n" + "unrelated tail " * 5
    result = chunk_for_context(text, query="needle", max_context_tokens=200)
    assert result.startswith("needle ")
    assert len(result) <= 700


def test_negative_context_budget_is_rejected_instead_of_returning_almost_full_text():
    with pytest.raises(ValueError, match="max_context_tokens"):
        chunk_for_context("x" * 500, max_context_tokens=-1)


def test_zero_context_budget_returns_empty():
    assert chunk_for_context("content", max_context_tokens=0) == ""
