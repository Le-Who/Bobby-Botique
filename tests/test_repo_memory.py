"""Tests for app.repos.memory — constants and configuration validation."""

import pytest


class TestMemoryConstants:
    """Verify memory repo configuration constants are sane."""

    def test_embedding_dimension_matches_gemini(self):
        from app.repos.memory import EMBEDDING_DIMENSION

        assert EMBEDDING_DIMENSION == 768  # gemini-embedding-2-preview default sweet spot dimension

    def test_max_memories_per_user_is_bounded(self):
        from app.repos.memory import MAX_MEMORIES_PER_USER

        assert 100 <= MAX_MEMORIES_PER_USER <= 10000

    def test_default_ttl_is_reasonable(self):
        from app.repos.memory import DEFAULT_MEMORY_TTL_DAYS

        assert 30 <= DEFAULT_MEMORY_TTL_DAYS <= 365

    def test_embedding_model_is_specified(self):
        from app.repos.memory import EMBEDDING_MODEL

        assert "gemini-embedding-2" in EMBEDDING_MODEL.lower()
