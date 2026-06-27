"""
Documents package — file processing, parsing, and persistence.

Re-exports everything for backward compatibility with::

    from app.document_processor import document_processor, process_uploaded_document, ...
"""

from app.documents.parsers import (  # noqa: F401
    MAX_DOCUMENT_TEXT_LENGTH,
    calculate_file_hash_sync,
    write_temp_file_sync,
)
from app.documents.repository import (  # noqa: F401
    check_document_limit,
    check_duplicate_file,
    cleanup_old_documents,
    cleanup_oldest_documents,
    delete_all_user_documents,
    delete_document,
    get_document_by_id,
    get_document_content,
    get_document_stats,
    get_user_document_count,
    get_user_document_stats,
    get_user_documents,
    save_document_content,
)
