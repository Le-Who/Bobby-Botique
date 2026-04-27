"""
Comprehensive AAA unit tests for app.thinking_classifier.

Covers:
- All HIGH signal categories (code, math, multipart, comparison, long, debug, architecture)
- All LOW signal categories (greeting, confirmation, short translation, single word)
- MEDIUM / grey-zone fallback
- Context-aware escalation from conversation history
- resolve_thinking_level user override and auto-mode
- Edge cases: empty string, whitespace, mixed signals
"""

import pytest

from app.thinking_classifier import classify_thinking_level, resolve_thinking_level

# ─── HIGH signals ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "```python\nprint(1)\n```",
        "def my_function():",
        "class Foo:",
        "import os",
        "from pathlib import Path",
        "#include <stdio.h>",
    ],
    ids=["fenced_code", "def", "class", "import", "from_import", "include"],
)
def test_high_code_signals(text):
    """Any code marker must yield 'high' thinking level."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high", f"Expected 'high' for code input: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "2 + 2 = 4",
        "∫x dx",
        "∑(1..n)",
        "$E = mc^2$",
    ],
    ids=["arithmetic", "integral", "sum", "latex"],
)
def test_high_math_signals(text):
    """Mathematical notation must yield 'high' thinking level."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high", f"Expected 'high' for math input: {text!r}"


def test_high_multipart_numbered_list_with_3_or_more_items():
    """3+ numbered items in text must yield 'high' thinking level."""
    # Arrange
    text = "Сделай следующее:\n1. Шаг один\n2. Шаг два\n3. Шаг три"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high"


def test_high_only_two_numbered_items_is_not_high():
    """Exactly 2 numbered items must NOT trigger multipart HIGH signal."""
    # Arrange
    text = "Сделай:\n1. Шаг один\n2. Шаг два"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result != "high", "2 items should not be enough for HIGH multipart signal"


def test_high_comparison_request():
    """Comparison keywords must yield 'high' thinking level."""
    # Arrange
    text = "сравни React vs Vue"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high"


def test_high_architecture_keyword():
    """Architecture/design keywords must yield 'high' thinking level."""
    # Arrange
    text = "как спроектировать архитектуру микросервисов?"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high"


def test_high_debug_keyword():
    """Debugging keywords must yield 'high' thinking level."""
    # Arrange
    text = "не работает функция, error при вызове"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high"


@pytest.mark.parametrize(
    "text",
    [
        "напиши сочинение на тему осень",
        "сделай мне реферат по истории",
        "подготовь доклад про космос",
        "нужно эссе для университета",
        "write a long dissertation",
        "сочини сказку", # this overlaps with the creative ones, but long form wins
        "can you write a report on this?",
    ],
    ids=["ru_essay", "ru_referat", "ru_report", "ru_essay2", "en_dissertation", "ru_compose", "en_report"],
)
def test_high_long_form_writing_signals(text):
    """Long-form writing requests must yield 'high' thinking level."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high", f"Expected 'high' for writing request: {text!r}"


def test_high_long_message_over_800_chars():
    """Messages over 800 characters must yield 'high' thinking level."""
    # Arrange
    text = "x " * 450  # 900 characters

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high"


def test_high_multiple_questions_three_or_more():
    """3 or more question marks must yield 'high' thinking level."""
    # Arrange
    text = "что это? зачем? как работает?"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "high"


# ─── LOW signals ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["привет!", "hello", "hi", "добрый день", "хей"],
    ids=["ru_hi", "en_hello", "en_hi", "ru_formal", "ru_hey"],
)
def test_low_greetings(text):
    """Greeting-only messages must yield 'low' thinking level."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "low", f"Expected 'low' for greeting: {text!r}"


@pytest.mark.parametrize(
    "text",
    ["да", "нет", "ок", "спасибо", "понял", "yes", "no", "ok", "thanks"],
    ids=["ru_yes", "ru_no", "ru_ok", "ru_thanks", "ru_got_it", "en_yes", "en_no", "en_ok", "en_thanks"],
)
def test_low_confirmations(text):
    """Short confirmations must yield 'low' thinking level."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "low", f"Expected 'low' for confirmation: {text!r}"


def test_low_single_word_no_space():
    """Single word under 20 chars without space must yield 'low'."""
    # Arrange
    text = "слово"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "low"


def test_low_short_translation_request():
    """Short translation request (< 60 chars, single ? or none) must yield 'low'."""
    # Arrange
    text = "переведи на английский"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "low"


# ─── MEDIUM / grey zone ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "объясни как работает GC в Python",
        "почему это происходит?",
        "как работает это?",
    ],
    ids=["explain_ru", "why_ru", "how_ru"],
)
def test_medium_explanation_requests(text):
    """Explanation requests without code/math must yield 'medium'."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "medium", f"Expected 'medium' for: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "напиши стихотворение про зиму",
        "придумай историю про кота",
    ],
    ids=["poem", "story"],
)
def test_medium_creative_requests(text):
    """Creative requests without code/math must yield 'medium'."""
    # Arrange / Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "medium", f"Expected 'medium' for creative: {text!r}"


def test_medium_summarize_keyword():
    """Summarization request must yield 'medium'."""
    # Arrange
    text = "кратко опиши процесс деплоя"

    # Act
    result = classify_thinking_level(text)

    # Assert
    assert result == "medium"


def test_medium_empty_string_returns_medium_or_low():
    """Empty string should not crash and should return a valid level."""
    # Arrange / Act
    result = classify_thinking_level("")

    # Assert
    assert result in ("low", "medium", "high")


# ─── Context-aware escalation ─────────────────────────────────────────────────


def test_high_context_escalation_from_long_model_responses():
    """3+ long model responses in history must escalate a MEDIUM grey-zone message to 'high'.

    Context escalation only applies to messages that reach Phase 3 (MEDIUM grey zone).
    We use a message with an 'объясни' keyword to guarantee MEDIUM classification
    without code/math (which would short-circuit to HIGH before context check).
    Word count > 4 words prevents LOW-short-simple classification.
    """
    # Arrange
    long_part = "x" * 2500  # > 2000 chars to trigger the escalation threshold
    history = [
        {"role": "model", "parts": [long_part]},
        {"role": "model", "parts": [long_part]},
        {"role": "model", "parts": [long_part]},
    ]
    # A message that lands in MEDIUM grey-zone:
    # - Has 'объясни' (MEDIUM_EXPLAIN trigger)
    # - > 60 chars to stay in grey-zone, not LOW-short
    message = "объясни мне детально, пожалуйста, как это вообще всё работает на практике"

    # Act
    result = classify_thinking_level(message, history=history)

    # Assert
    assert result == "high", "Complex conversation context should escalate MEDIUM to 'high'"


def test_no_escalation_with_short_model_responses():
    """Short model responses in history must not escalate a MEDIUM grey-zone message."""
    # Arrange
    history = [
        {"role": "model", "parts": ["Да, конечно."]},
        {"role": "model", "parts": ["Хорошо."]},
        {"role": "model", "parts": ["Понял."]},
    ]
    # Same MEDIUM message as in escalation test, but with short history
    message = "объясни мне детально, пожалуйста, как это вообще всё работает на практике"

    # Act
    result = classify_thinking_level(message, history=history)

    # Assert
    assert result in ("low", "medium"), "Short model responses should not trigger context escalation"


def test_context_escalation_requires_at_least_3_long_responses():
    """Only 2 long model responses must NOT trigger escalation to 'high'."""
    # Arrange
    long_part = "x" * 2500
    history = [
        {"role": "model", "parts": [long_part]},
        {"role": "model", "parts": [long_part]},
    ]
    # Same MEDIUM-zone message as in other context tests
    message = "объясни мне детально, пожалуйста, как это вообще всё работает на практике"

    # Act
    result = classify_thinking_level(message, history=history)

    # Assert
    assert result != "high", "2 long responses should not be enough for context escalation"


# ─── resolve_thinking_level ───────────────────────────────────────────────────


@pytest.mark.parametrize("explicit_level", ["low", "medium", "high"])
def test_resolve_explicit_user_level_always_wins(explicit_level):
    """Explicit user setting must override auto-classification for any message."""
    # Arrange
    greedy_message = "```python\ncomplex code\n```"  # Would be 'high' if auto

    # Act
    result = resolve_thinking_level(explicit_level, greedy_message)

    # Assert
    assert result == explicit_level, f"Explicit '{explicit_level}' must not be overridden"


def test_resolve_off_returns_none():
    """'off' setting must disable thinking by returning None."""
    # Arrange
    complex_message = "```python\ncomplex code\n```"

    # Act
    result = resolve_thinking_level("off", complex_message)

    # Assert
    assert result is None


def test_resolve_none_triggers_auto_classification():
    """None user level must trigger auto classification."""
    # Arrange
    greeting = "привет"

    # Act
    result = resolve_thinking_level(None, greeting)

    # Assert
    assert result == "low"


def test_resolve_auto_string_triggers_auto_classification():
    """'auto' string must behave identically to None."""
    # Arrange
    greeting = "привет"

    # Act
    result = resolve_thinking_level("auto", greeting)

    # Assert
    assert result == "low"


def test_resolve_unknown_user_level_falls_through_to_auto():
    """Unknown/invalid user level must fall through to auto classification."""
    # Arrange
    greeting = "привет"

    # Act
    result = resolve_thinking_level("unknown_level", greeting)

    # Assert
    assert result in ("low", "medium", "high"), "Invalid level should auto-classify, not crash"
