"""Tests for app.repos.chats — message extraction, chat state helpers."""


from app.repos.chats import _extract_message_content


class TestExtractMessageContent:
    """Test the pure-logic message content extractor."""

    def test_string_content(self):
        msg = {"content": "Hello world"}
        assert _extract_message_content(msg) == "Hello world"

    def test_list_content(self):
        msg = {"content": ["Part 1", "Part 2"]}
        assert _extract_message_content(msg) == "Part 1 Part 2"

    def test_parts_string_list(self):
        msg = {"parts": ["Hello", "World"]}
        assert _extract_message_content(msg) == "Hello World"

    def test_parts_with_dict_items(self):
        msg = {"parts": [{"text": "Chunk 1"}, {"text": "Chunk 2"}]}
        result = _extract_message_content(msg)
        assert "Chunk 1" in result
        assert "Chunk 2" in result

    def test_empty_message(self):
        assert _extract_message_content({}) == ""

    def test_none_content_coerced(self):
        msg = {"content": None}
        assert _extract_message_content(msg) == "None"

    def test_parts_as_string(self):
        msg = {"parts": "single string"}
        assert _extract_message_content(msg) == "single string"

    def test_content_takes_precedence_over_parts(self):
        """When both 'content' and 'parts' exist, 'content' should be used."""
        msg = {"content": "from content", "parts": ["from parts"]}
        assert _extract_message_content(msg) == "from content"
