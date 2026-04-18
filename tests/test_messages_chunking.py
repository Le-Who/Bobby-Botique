import pytest
from app.handlers.messages import chunk_message

def test_chunk_message_empty():
    assert chunk_message("") == []

def test_chunk_message_shorter_than_max():
    text = "Hello world"
    assert chunk_message(text, max_length=20) == ["Hello world"]

def test_chunk_message_exactly_max():
    text = "A" * 10
    assert chunk_message(text, max_length=10) == [text]

def test_chunk_message_invalid_max_length():
    with pytest.raises(ValueError, match="max_length must be > 0"):
        chunk_message("Hello", max_length=0)

    with pytest.raises(ValueError, match="max_length must be > 0"):
        chunk_message("Hello", max_length=-5)

def test_chunk_message_split_by_newline():
    text = "Line 1\nLine 2\nLine 3"
    # max_length=7 can fit "Line 1\n" (7 chars) and "Line 2\n" and "Line 3"
    chunks = chunk_message(text, max_length=7)
    assert chunks == ["Line 1", "Line 2", "Line 3"]

def test_chunk_message_split_by_space():
    text = "Word1 Word2 Word3"
    chunks = chunk_message(text, max_length=6)
    assert chunks == ["Word1", "Word2", "Word3"]

def test_chunk_message_hard_split():
    text = "A" * 15
    chunks = chunk_message(text, max_length=5)
    assert chunks == ["AAAAA", "AAAAA", "AAAAA"]

def test_chunk_message_trailing_whitespace():
    text = "Chunk1 \n Chunk2  "
    chunks = chunk_message(text, max_length=7)
    # The split happens, and chunks are stripped
    assert chunks == ["Chunk1", "Chunk2"]

def test_chunk_message_large_text():
    # Example where a very long text requires multiple chunking strategies
    text = "A"*10 + " " + "B"*10 + "\n" + "C"*10
    # max_length=15
    chunks = chunk_message(text, max_length=15)
    # 1. "A"*10 + " " + "B"*10 -> space is at index 10.
    # So chunk 1 takes "A"*10 (length 10). Next part: "B"*10 + "\n" + "C"*10
    # 2. "B"*10 + "\n" + "C"*10 -> newline at index 10.
    # So chunk 2 takes "B"*10 (length 10). Next part: "C"*10
    # 3. Chunk 3 takes "C"*10.
    assert chunks == ["A"*10, "B"*10, "C"*10]

def test_chunk_message_split_exact_space():
    # If a space is exactly at max_length, it should correctly split there
    text = "A"*5 + " " + "B"*5
    chunks = chunk_message(text, max_length=6)
    assert chunks == ["A"*5, "B"*5]

def test_chunk_message_multiple_newlines():
    text = "A\n\nB"
    # length of A\n\n is 3. Max length 2 -> can take A\n
    # Next part is \nB, stripped -> B
    chunks = chunk_message(text, max_length=2)
    assert chunks == ["A", "B"]
