import time
import pytest
from app import prompts

def test_create_conversation_summary_performance():
    # Setup large history
    large_history = []
    message_content = "This is a long message " * 100 # 2300 chars
    # 1000 messages * 2300 chars = 2.3M chars
    for i in range(1000):
        large_history.append({'role': 'user', 'parts': [message_content]})
        large_history.append({'role': 'model', 'parts': [message_content]})

    start_time = time.time()
    summary = prompts.create_conversation_summary(large_history)
    end_time = time.time()

    print(f"Summary length: {len(summary)}")
    print(f"Time taken: {end_time - start_time:.6f} seconds")

    # Assert correctness
    assert summary.startswith("Предыдущий контекст беседы:\n")
    # 2000 chars for content + "..." + prefix
    expected_max_len = 2000 + 3 + len("Предыдущий контекст беседы:\n")
    assert len(summary) <= expected_max_len

    # Check content integrity (at least start matches)
    assert "Пользователь: This is a long message" in summary
