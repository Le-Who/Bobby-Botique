"""Tests for hidden response-tag parsing."""

from app.utils.response_tags import extract_suggestions


def test_suggestion_ids_use_sha256_prefix():
    cleaned, suggestions = extract_suggestions("Ответ [SUGGESTIONS: hello]")

    assert cleaned == "Ответ "
    assert suggestions == [{"id": "2cf24dba5f", "label": "hello"}]
