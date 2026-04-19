import pytest

from app.handlers.inline import parse_inline_query


def test_parse_inline_query_empty():
    assert parse_inline_query("") == {}


def test_parse_inline_query_basic():
    res = parse_inline_query("hello world")
    assert res["is_image_intent"] is False
    assert res["stripped_prompt"] == "hello world"


def test_parse_inline_query_image_intent():
    res = parse_inline_query("нарисуй кота")
    assert res["is_image_intent"] is True
    assert res["stripped_prompt"] == "кота"
    assert res["has_edit_intent"] is False
    assert res["has_quoted_text"] is False


def test_parse_inline_query_image_intent_quoted():
    res = parse_inline_query('нарисуй кота с надписью "привет"')
    assert res["is_image_intent"] is True
    assert res["has_quoted_text"] is True


def test_parse_inline_query_edit_intent():
    # Use an edit keyword, e.g. "измени"
    res = parse_inline_query("измени фото")
    assert res["is_image_intent"] is True
    assert res["has_edit_intent"] is True
