"""Pure response-presentation normalization tests."""

from app.response_delivery.presentation import FixedPresentation, PresentationFacts


def _prepare(text: str) -> str:
    return FixedPresentation().prepare(
        PresentationFacts(raw_content=text, terminal=None, voice_requested=False)
    ).content_text


def test_internal_google_search_tool_trace_is_removed_without_losing_answer_tail():
    raw = '[tool_code]\nimport google_search\ngoogle_search.search("cats")\nreal answer'

    assert _prepare(raw) == "real answer"


def test_legitimate_search_code_and_prose_are_preserved():
    fenced = '```python\nsearch("cats")\n```\nreal answer'
    prose = 'Use `google_search.search("cats")` as an example.'

    assert _prepare(fenced) == fenced
    assert _prepare(prose) == prose
