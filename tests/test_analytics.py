import sys
from unittest.mock import MagicMock


# We mock at the top level to allow imports to succeed in isolated test runs,
# but we shouldn't pollute the global sys.modules permanently if this runs in a suite.
# However, for this environment, it's a known pattern. We will use a context manager or
# just keep the mock localized to this test file. Wait, if it's imported at the top level
# of analytics.py we have to mock it before importing analytics.py.
# A cleaner way is using `unittest.mock.patch.dict` or just leaving it since it's common
# in this codebase's restricted env, but to be perfectly clean:
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = MagicMock()
if "app.database" not in sys.modules:
    sys.modules["app.database"] = MagicMock()

from app.repos.analytics import generate_auto_title


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
