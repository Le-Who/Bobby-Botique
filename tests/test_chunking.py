"""Tests for document chunking module.

Validates:
1. Empty/short text returns as-is.
2. Recursive chunking splits at paragraph/sentence boundaries.
3. Overlap is present between chunks.
4. Hierarchical chunking creates parent/child structure.
5. chunk_for_context: short docs pass through, long docs get scored and truncated.
6. Query relevance scoring selects better chunks.
"""

from app.documents.chunking import (
    _estimate_tokens,
    _score_chunk,
    chunk_for_context,
    hierarchical_chunk,
    recursive_chunk,
)


class TestRecursiveChunk:
    """Test recursive_chunk function."""

    def test_empty_text(self):
        assert recursive_chunk("") == []
        assert recursive_chunk("   ") == []

    def test_short_text_returns_single_chunk(self):
        text = "Hello world. This is a short text."
        chunks = recursive_chunk(text, max_tokens=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_splits_by_paragraphs(self):
        """Paragraphs separated by double newline should be split boundaries."""
        paragraphs = ["Paragraph " + str(i) + ". " + "x" * 200 for i in range(5)]
        text = "\n\n".join(paragraphs)
        chunks = recursive_chunk(text, max_tokens=100)
        assert len(chunks) > 1

    def test_all_chunks_within_budget(self):
        """Every chunk should be within max_tokens (approximately)."""
        text = "Word. " * 1000
        chunks = recursive_chunk(text, max_tokens=50)
        max_chars = int(50 * 3.5 * 1.3)  # 30% tolerance for overlap
        for chunk in chunks:
            assert len(chunk) <= max_chars, f"Chunk too large: {len(chunk)} > {max_chars}"

    def test_overlap_present(self):
        """Consecutive chunks should share some text (overlap)."""
        text = "Sentence one. " * 100
        chunks = recursive_chunk(text, max_tokens=30, overlap_ratio=0.2)
        if len(chunks) >= 2:
            # Last part of chunk[0] should appear in beginning of chunk[1]
            tail = chunks[0][-20:]
            assert tail in chunks[1] or any(word in chunks[1][:50] for word in tail.split()), (
                "No overlap detected between consecutive chunks"
            )


class TestHierarchicalChunk:
    """Test hierarchical_chunk function."""

    def test_empty_text(self):
        assert hierarchical_chunk("") == []

    def test_short_text_single_parent(self):
        text = "Short text for testing."
        result = hierarchical_chunk(text, child_tokens=100, parent_tokens=200)
        assert len(result) == 1
        assert result[0].parent == text
        assert len(result[0].children) >= 1

    def test_long_text_multiple_parents(self):
        text = "Content block. " * 500
        result = hierarchical_chunk(text, child_tokens=50, parent_tokens=200)
        assert len(result) > 1
        for hc in result:
            assert len(hc.children) >= 1
            assert len(hc.parent) > 0


class TestScoreChunk:
    """Test relevance scoring."""

    def test_no_query_returns_half(self):
        assert _score_chunk("any text here", "") == 0.5

    def test_full_match(self):
        chunk = "Python programming language tutorial"
        query = "python programming"
        score = _score_chunk(chunk, query)
        assert score == 1.0

    def test_partial_match(self):
        chunk = "Python is great for web development"
        query = "python machine learning"
        score = _score_chunk(chunk, query)
        assert 0.0 < score < 1.0

    def test_no_match(self):
        chunk = "The quick brown fox jumps"
        query = "quantum computing algorithms"
        score = _score_chunk(chunk, query)
        assert score == 0.0


class TestChunkForContext:
    """Test the main entry point."""

    def test_empty_text(self):
        assert chunk_for_context("") == ""
        assert chunk_for_context("   ") == ""

    def test_short_doc_passes_through(self):
        text = "This is a short document that fits in context."
        result = chunk_for_context(text, max_context_tokens=1000)
        assert result == text

    def test_long_doc_truncated_to_budget(self):
        text = "Content block. " * 5000
        result = chunk_for_context(text, max_context_tokens=500)
        result_tokens = _estimate_tokens(result)
        # Should be within budget (some tolerance)
        assert result_tokens <= 600  # 500 + tolerance

    def test_query_relevance_affects_selection(self):
        """Chunks matching the query should be selected over non-matching."""
        # Build doc with distinct sections
        section_a = "Machine learning models and neural networks. " * 100
        section_b = "Cooking recipes for pasta and pizza. " * 100
        section_c = "Database optimization and query performance. " * 100
        text = section_a + "\n\n" + section_b + "\n\n" + section_c

        result = chunk_for_context(text, query="database query optimization", max_context_tokens=200)
        # The result should contain database-related content
        assert "database" in result.lower() or "query" in result.lower() or "optimization" in result.lower()
