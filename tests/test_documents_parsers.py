"""Tests for app.documents.parsers — sync file I/O helpers."""

import hashlib
import os
import tempfile

from app.documents.parsers import (
    MAX_DOCUMENT_TEXT_LENGTH,
    calculate_file_hash_sync,
    write_temp_file_sync,
)


# ── Constants ─────────────────────────────────────────────────────────────────


class TestConstants:
    def test_max_document_text_length(self):
        assert MAX_DOCUMENT_TEXT_LENGTH == 100_000


# ── write_temp_file_sync ──────────────────────────────────────────────────────


class TestWriteTempFileSync:
    def test_creates_file_with_correct_content(self):
        data = b"Hello, World!"
        path = write_temp_file_sync(data, ".txt")
        try:
            assert os.path.exists(path)
            with open(path, "rb") as f:
                assert f.read() == data
        finally:
            os.unlink(path)

    def test_creates_file_with_correct_suffix(self):
        path = write_temp_file_sync(b"test", ".pdf")
        try:
            assert path.endswith(".pdf")
        finally:
            os.unlink(path)

    def test_empty_data(self):
        path = write_temp_file_sync(b"", ".bin")
        try:
            assert os.path.getsize(path) == 0
        finally:
            os.unlink(path)

    def test_large_data(self):
        data = b"x" * (1024 * 1024)  # 1MB
        path = write_temp_file_sync(data, ".dat")
        try:
            assert os.path.getsize(path) == 1024 * 1024
        finally:
            os.unlink(path)


# ── calculate_file_hash_sync ──────────────────────────────────────────────────


class TestCalculateFileHashSync:
    def test_hash_bytes_deterministic(self):
        data = b"consistent input"
        h1 = calculate_file_hash_sync(data)
        h2 = calculate_file_hash_sync(data)
        assert h1 == h2

    def test_hash_bytes_correct(self):
        data = b"test data"
        expected = hashlib.sha256(data).hexdigest()
        assert calculate_file_hash_sync(data) == expected

    def test_hash_different_data_differs(self):
        h1 = calculate_file_hash_sync(b"data1")
        h2 = calculate_file_hash_sync(b"data2")
        assert h1 != h2

    def test_hash_file_path(self):
        data = b"file content for hashing"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            path = f.name
        try:
            h_path = calculate_file_hash_sync(path)
            h_bytes = calculate_file_hash_sync(data)
            assert h_path == h_bytes
        finally:
            os.unlink(path)

    def test_hash_empty_bytes(self):
        h = calculate_file_hash_sync(b"")
        assert h == hashlib.sha256(b"").hexdigest()

    def test_hash_returns_hex_string(self):
        h = calculate_file_hash_sync(b"abc")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex = 64 chars
        assert all(c in "0123456789abcdef" for c in h)
