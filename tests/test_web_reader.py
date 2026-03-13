"""Tests for app.web_reader — URL reading and truncation."""

from app.web_reader import MAX_PAGE_CHARS


class TestWebReaderConstants:
    """Verify web reader configuration constants."""

    def test_max_page_chars_reasonable(self):
        assert MAX_PAGE_CHARS > 1000
        assert MAX_PAGE_CHARS < 100_000
