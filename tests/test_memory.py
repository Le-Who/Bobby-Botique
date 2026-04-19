import pytest
from app.repos.memory import _should_expand_query

def test_should_expand_query_too_short():
    """Test that queries shorter than the minimum length are not expanded."""
    assert _should_expand_query("") is False
    assert _should_expand_query("hi") is False
    assert _should_expand_query("short query") is False
    assert _should_expand_query("12345678901") is False

def test_should_expand_query_trivial_matches():
    """Test that trivial queries (greetings, confirmations, emojis) are not expanded even if long."""
    assert _should_expand_query("hello!!!!!!!") is False
    assert _should_expand_query("thanks.......") is False
    assert _should_expand_query("спасибо!!!!!") is False
    assert _should_expand_query("ok!!!!!!!!!!") is False
    assert _should_expand_query("👋!!!!!!!!!!!") is False
    assert _should_expand_query("  yo.........  ") is False

def test_should_expand_query_valid():
    """Test that non-trivial queries of sufficient length are expanded."""
    assert _should_expand_query("what is my name?") is True
    assert _should_expand_query("tell me about python") is True
    assert _should_expand_query("123456789012") is True
    assert _should_expand_query("hello there, world!") is True

def test_should_expand_query_strip():
    """Test that whitespace is stripped before checking length."""
    # Length is > 12 with whitespace, but < 12 when stripped
    assert _should_expand_query("   short    ") is False
