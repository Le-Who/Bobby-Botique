"""Quick smoke test for the thinking classifier."""

from app.thinking_classifier import classify_thinking_level, resolve_thinking_level


def test_high_code():
    assert classify_thinking_level("```python\nprint(1)\n```") == "high"


def test_high_math():
    assert classify_thinking_level("2 + 2 = 4") == "high"


def test_high_comparison():
    assert classify_thinking_level("сравни react vs vue") == "high"


def test_high_architecture():
    assert classify_thinking_level("как спроектировать архитектуру микросервисов?") == "high"


def test_high_debug():
    assert classify_thinking_level("не работает функция, error при вызове") == "high"


def test_high_long_message():
    assert classify_thinking_level("x " * 500) == "high"


def test_high_multipart():
    text = "Сделай следующее:\n1. Создай файл\n2. Напиши код\n3. Запусти тесты\n4. Проверь результат"
    assert classify_thinking_level(text) == "high"


def test_high_multiple_questions():
    assert classify_thinking_level("первый? второй? третий?") == "high"


def test_low_greeting():
    assert classify_thinking_level("привет!") == "low"


def test_low_confirmation():
    assert classify_thinking_level("ок") == "low"
    assert classify_thinking_level("да") == "low"
    assert classify_thinking_level("спасибо") == "low"


def test_low_single_word():
    assert classify_thinking_level("привет") == "low"


def test_medium_explain():
    assert classify_thinking_level("объясни как работает GC в Python") == "medium"


def test_medium_creative():
    assert classify_thinking_level("напиши стихотворение про зиму") == "medium"


def test_medium_summarize():
    assert classify_thinking_level("кратко опиши процесс") == "medium"


# --- resolve tests ---


def test_resolve_user_override_high():
    assert resolve_thinking_level("high", "привет") == "high"


def test_resolve_user_override_off():
    assert resolve_thinking_level("off", "сложная задача") is None


def test_resolve_auto_low():
    assert resolve_thinking_level(None, "привет") == "low"


def test_resolve_auto_high():
    assert resolve_thinking_level(None, "```python\nprint(1)\n```") == "high"


def test_resolve_auto_medium():
    assert resolve_thinking_level(None, "объясни как работает GC") == "medium"


def test_resolve_explicit_auto_string():
    assert resolve_thinking_level("auto", "привет") == "low"
