import sys
from unittest.mock import MagicMock

import pytest

# Isolate in dedicated xdist worker — this file mutates sys.modules in setup_module.
pytestmark = pytest.mark.xdist_group("sys_modules_isolation")

_MOCK_KEYS = ["asyncpg", "app.database"]
_original_modules: dict = {}

# Populated by setup_module after mocks are installed. All tests use this reference.
generate_auto_title = None


def setup_module(module):
    global _original_modules, generate_auto_title
    _original_modules["__app_keys_before__"] = {
        k for k in sys.modules if k.startswith("app.")
    }
    for k in _MOCK_KEYS:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()

    # Force-reimport analytics with mocked deps so it resolves cleanly.
    # Remove any cached import from collection time first.
    sys.modules.pop("app.repos.analytics", None)
    from app.repos.analytics import generate_auto_title as _fn

    generate_auto_title = _fn


def teardown_module(module):
    app_keys_before = _original_modules.pop("__app_keys_before__", set())
    for k in _MOCK_KEYS:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)
    for k in list(sys.modules):
        if k.startswith("app.") and k not in app_keys_before:
            del sys.modules[k]



def test_generate_auto_title_simple_content():
    messages = [{"role": "user", "content": "Hello world!"}]
    assert generate_auto_title(messages) == "Hello world!"


def test_generate_auto_title_skips_non_user():
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "assistant", "content": "How can I help you?"},
        {"role": "user", "content": "My actual message."},
    ]
    assert generate_auto_title(messages) == "My actual message."


def test_generate_auto_title_with_parts_strings():
    messages = [{"role": "user", "parts": ["Hello", "world"]}]
    assert generate_auto_title(messages) == "Hello world"


def test_generate_auto_title_with_parts_dicts():
    messages = [{"role": "user", "parts": [{"text": "First part"}, {"text": "Second part"}]}]
    assert generate_auto_title(messages) == "First part Second part"


def test_generate_auto_title_with_mixed_parts():
    messages = [
        {
            "role": "user",
            "parts": ["Hello", {"image": b"binary_data"}, {"text": "there"}],
        }
    ]
    assert generate_auto_title(messages) == "Hello there"


def test_generate_auto_title_empty_content():
    messages = [
        {"role": "user", "content": "   \n  "},
        {"role": "user", "content": "Valid message"},
    ]
    assert generate_auto_title(messages) == "Valid message"


def test_generate_auto_title_empty_parts():
    messages = [
        {"role": "user", "parts": [{"image": b"123"}]},
        {"role": "user", "content": "Valid message"},
    ]
    assert generate_auto_title(messages) == "Valid message"


def test_generate_auto_title_truncates_long_string():
    long_msg = "a" * 100
    messages = [{"role": "user", "content": long_msg}]
    title = generate_auto_title(messages, max_len=60)
    assert len(title) == 60
    assert title.endswith("...")
    assert title == "a" * 57 + "..."


def test_generate_auto_title_truncates_at_sentence_boundary_dot():
    # sentence boundary at index 14
    messages = [{"role": "user", "content": "This is a test. Here is more text."}]
    title = generate_auto_title(messages, max_len=60)
    assert title == "This is a test."


def test_generate_auto_title_truncates_at_sentence_boundary_question():
    # . is earlier in the list than ?, so if a . occurs, it triggers first
    # let's only use ? to trigger the ? boundary
    messages = [{"role": "user", "content": "What is your name? Tell me more"}]
    title = generate_auto_title(messages, max_len=60)
    assert title == "What is your name?"


def test_generate_auto_title_truncates_at_sentence_boundary_exclamation():
    messages = [{"role": "user", "content": "Wow amazing! Thanks"}]
    title = generate_auto_title(messages, max_len=60)
    assert title == "Wow amazing!"


def test_generate_auto_title_truncates_at_sentence_boundary_newline():
    messages = [{"role": "user", "content": "Line 1\nLine 2\nLine 3"}]
    title = generate_auto_title(messages, max_len=60)
    assert title == "Line 1"


def test_generate_auto_title_ignores_early_sentence_boundary():
    # Boundary at index 3 (< 5) should be ignored
    messages = [{"role": "user", "content": "Hi. My name is Jules."}]
    title = generate_auto_title(messages, max_len=60)
    assert title == "Hi. My name is Jules."


def test_generate_auto_title_sentence_boundary_after_max_len():
    # Boundary happens after max_len, so it should just truncate at max_len
    messages = [
        {
            "role": "user",
            "content": "This is a very long sentence that has no boundaries and will exceed twenty characters. Right?",
        }
    ]
    title = generate_auto_title(messages, max_len=20)
    assert title == "This is a very lo..."


def test_generate_auto_title_fallback():
    messages = [{"role": "system", "content": "Only system messages."}]
    title = generate_auto_title(messages)
    assert title.startswith("Беседа от ")
    assert len(title) == len("Беседа от 25.10.2023 14:30")


def test_generate_auto_title_empty_messages_list():
    title = generate_auto_title([])
    assert title.startswith("Беседа от ")
    assert len(title) == len("Беседа от 25.10.2023 14:30")
