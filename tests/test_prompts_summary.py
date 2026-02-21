
import pytest
from app.prompts import create_conversation_summary

def test_create_conversation_summary_empty():
    assert create_conversation_summary([]) == ""

def test_create_conversation_summary_basic():
    messages = [
        {'role': 'user', 'parts': ['Hello']},
        {'role': 'model', 'parts': ['Hi there']}
    ]
    summary = create_conversation_summary(messages)
    expected = "Предыдущий контекст беседы:\nПользователь: Hello \nАссистент: Hi there \n"
    assert summary == expected

def test_create_conversation_summary_truncation():
    # Create messages that exceed 2000 chars
    long_text = "a" * 1000
    messages = [
        {'role': 'user', 'parts': [long_text]},
        {'role': 'model', 'parts': [long_text]},
        {'role': 'user', 'parts': ["Should not be here"]}
    ]

    summary = create_conversation_summary(messages)

    assert "Предыдущий контекст беседы:" in summary
    # The summary body (after the header) should be truncated
    body = summary.replace("Предыдущий контекст беседы:\n", "")
    assert len(body) == 2003 # 2000 + "..."
    assert body.endswith("...")
    assert "Should not be here" not in body

def test_create_conversation_summary_mixed_parts():
    messages = [
        {'role': 'user', 'parts': ['Part 1', 'Part 2']},
    ]
    summary = create_conversation_summary(messages)
    assert "Part 1 Part 2" in summary

def test_create_conversation_summary_custom_role():
    messages = [
        {'role': 'teacher', 'parts': ['Lesson 1']},
    ]
    summary = create_conversation_summary(messages)
    assert "teacher: Lesson 1" in summary
