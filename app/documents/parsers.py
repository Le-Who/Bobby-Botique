"""
Document file parsers — CPU-bound PDF and DOCX processing.

Extracted from ``DocumentProcessor`` to isolate file I/O and parsing
from database persistence and orchestration.
"""

import hashlib
import logging
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

# Maximum characters to extract from a document to prevent OOM
MAX_DOCUMENT_TEXT_LENGTH = 100000


def write_temp_file_sync(file_data: bytes, suffix: str) -> str:
    """Write data to a temp file and return its path (sync, thread-safe)."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_file.write(file_data)
        return temp_file.name


def calculate_file_hash_sync(file_path_or_data: str | bytes) -> str:
    """Compute SHA-256 hash of file data or a file path (streaming)."""
    if isinstance(file_path_or_data, bytes):
        return hashlib.sha256(file_path_or_data).hexdigest()

    h = hashlib.sha256()
    with open(file_path_or_data, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
