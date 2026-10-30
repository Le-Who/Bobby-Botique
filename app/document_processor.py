"""
Document processor — orchestration layer for document upload and processing.

The heavy lifting is delegated to submodules:
- ``app.documents.parsers``:    sync file I/O and hashing
- ``app.documents.repository``: async database CRUD and stats

This file keeps the ``DocumentProcessor`` class (orchestrator + PDF/DOCX
parsing), the singleton instance, and handler-facing facade functions.
"""

import asyncio
import io
import logging
from pathlib import Path
from typing import Any

import asyncpg
import pypdf
from docx import Document as DocxDocument

# Submodule imports
from app.documents.parsers import (
    MAX_DOCUMENT_TEXT_LENGTH,  # noqa: F401 — re-exported
    calculate_file_hash_sync,
)
from app.documents.repository import check_duplicate_file, save_document_content
from app.metrics import metrics_collector


def _is_transient_storage_error(error: Exception) -> bool:
    """Explicit operational failures; schema, permission and generic PG errors stay terminal."""
    return isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            asyncpg.PostgresConnectionError,
            asyncpg.CannotConnectNowError,
            asyncpg.TooManyConnectionsError,
            asyncpg.AdminShutdownError,
            asyncpg.CrashShutdownError,
            asyncpg.SerializationError,
            asyncpg.DeadlockDetectedError,
        ),
    )


def _duplicate_result(filename: str, duplicate: dict[str, Any]) -> dict[str, Any]:
    created_date = duplicate["created_at"]
    date_str = created_date.strftime("%Y-%m-%d") if hasattr(created_date, "strftime") else str(created_date)[:10]
    return {
        "error": "duplicate",
        "message": f"Файл '{filename}' уже был загружен ранее как '{duplicate['filename']}' ({date_str})",
        "duplicate_info": duplicate,
    }


class DocumentProcessor:
    """Orchestrates document upload, validation, parsing, and persistence."""

    def __init__(self):
        self.supported_formats = [".pdf", ".docx", ".doc"]
        self.max_file_size = 10 * 1024 * 1024  # 10MB for free tier
        self.max_pages = 50

    async def process_document(
        self, file_data, filename: str, user_id: int, is_path: bool = False, *, skip_duplicate_check: bool = False
    ) -> dict[str, Any]:
        """Process a document and return extracted text."""
        stage = "validation"
        write_started = False
        try:
            # Check file size
            if is_path:
                import os

                file_size = os.path.getsize(file_data)
            else:
                file_size = len(file_data)

            if file_size > self.max_file_size:
                return {"error": f"File too large. Maximum size is {self.max_file_size // (1024 * 1024)}MB"}

            file_ext = Path(filename).suffix.lower()
            if file_ext not in self.supported_formats:
                return {"error": f"Unsupported file format: {file_ext}"}

            # Hash + duplicate check
            loop = asyncio.get_running_loop()
            file_hash = await loop.run_in_executor(None, calculate_file_hash_sync, file_data)
            stage = "duplicate_lookup"
            duplicate = (
                None
                if skip_duplicate_check
                else await check_duplicate_file(user_id, file_hash, filename, raise_on_error=True)
            )

            if duplicate:
                return _duplicate_result(filename, duplicate)

            # Dispatch to format-specific parser
            stage = "parsing"
            if file_ext == ".pdf":
                result = await self._process_pdf_unified(file_data, filename, is_path=is_path)
            else:
                result = await self._process_word_unified(file_data, filename, is_path=is_path)
            if result.get("error"):
                return result
            from app.queue import current_job_guard

            guard = current_job_guard()
            if guard is not None:
                await guard.checkpoint()
            stage = "storage_write"
            # The repository owns the transaction. Once entered, a failed commit
            # reply cannot establish whether persistence took effect.
            write_started = True
            duplicate = await save_document_content(
                user_id,
                filename,
                result["content"],
                result["pages"],
                file_hash,
                skip_duplicate_check=skip_duplicate_check,
            )
            return _duplicate_result(filename, duplicate) if duplicate else result

        except (asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError, ConnectionError) as e:
            logging.error("Error saving uploaded document for user %s: %s", user_id, type(e).__name__)
            await metrics_collector.record_error("document_storage", type(e).__name__)
            return {
                "error": "storage_error",
                "error_stage": stage,
                "write_started": write_started,
                "retryable": stage == "duplicate_lookup" and not write_started and _is_transient_storage_error(e),
            }
        except (ValueError, UnicodeDecodeError, OSError) as e:
            logging.error("Error processing document %s: %s", filename, e, exc_info=True)
            await metrics_collector.record_error("document_processing", str(e))
            return {
                "error": f"Error processing document: {str(e)}",
                "error_stage": stage,
                "write_started": write_started,
                "retryable": False,
            }

    # ── Synchronous parsing helpers (run via run_in_executor) ─────────────

    @staticmethod
    def _process_pdf_sync(input_data: str | io.BytesIO, max_pages: int) -> dict[str, Any]:
        """Synchronous PDF text extraction — runs in a thread executor."""
        pdf_file = None
        should_close = False

        try:
            if isinstance(input_data, str):
                pdf_file = open(input_data, "rb")  # noqa: SIM115
                should_close = True
                stream = pdf_file
            else:
                stream = input_data  # type: ignore[assignment]  # BytesIO is compatible with PdfReader

            pdf_reader = pypdf.PdfReader(stream)

            if len(pdf_reader.pages) > max_pages:
                return {"error": f"PDF too large. Maximum {max_pages} pages allowed"}

            text_content: list[str] = []
            current_length = 0

            for page_num, page in enumerate(pdf_reader.pages):
                try:
                    text = page.extract_text()
                    if text.strip():
                        chunk = f"--- Page {page_num + 1} ---\n{text}"
                        text_content.append(chunk)
                        current_length += len(chunk)
                except Exception as page_error:
                    logging.warning(
                        "Error extracting text from page %d: %s",
                        page_num + 1,
                        page_error,
                    )
                    chunk = f"--- Page {page_num + 1} ---\n[Error extracting text from this page]"
                    text_content.append(chunk)
                    current_length += len(chunk)

                if current_length > MAX_DOCUMENT_TEXT_LENGTH:
                    break

            full_text = "\n\n".join(text_content)
            truncated = len(full_text) > MAX_DOCUMENT_TEXT_LENGTH
            full_text = full_text[:MAX_DOCUMENT_TEXT_LENGTH]
            return {
                "success": True,
                "pages": len(pdf_reader.pages),
                "content": full_text,
                "truncated": truncated,
            }
        except Exception as e:
            return {"error": str(e)}
        finally:
            if should_close and pdf_file:
                pdf_file.close()

    @staticmethod
    def _process_word_sync(input_data: str | io.BytesIO) -> dict[str, Any]:
        """Synchronous Word text extraction — runs in a thread executor."""
        try:
            doc = DocxDocument(input_data)

            text_content: list[str] = []
            paragraph_count = 0
            table_count = 0
            current_length = 0
            truncated = False

            def chunks():
                nonlocal paragraph_count, table_count
                for para in doc.paragraphs:
                    text = para.text
                    if text.strip():
                        paragraph_count += 1
                        yield text
                for table in doc.tables:
                    table_count += 1
                    yield f"\n--- Table {table_count} ---"
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        row_text = [text for text in cells if text]
                        if row_text:
                            yield " | ".join(row_text)

            for chunk in chunks():
                segment = ("\n\n" if text_content else "") + chunk
                remaining = MAX_DOCUMENT_TEXT_LENGTH - current_length
                if len(segment) > remaining:
                    text_content.append(segment[:remaining])
                    truncated = True
                    break
                text_content.append(segment)
                current_length += len(segment)

            full_text = "".join(text_content)
            return {
                "success": True,
                "pages": 1,
                "paragraphs": paragraph_count,
                "tables": table_count,
                "text_length": len(full_text),
                "content": full_text,
                "truncated": truncated,
            }
        except Exception as e:
            return {"error": str(e)}

    # ── PDF parsing ──────────────────────────────────────────────────────────

    async def _process_pdf_unified(
        self,
        file_data,
        filename: str,
        is_path: bool = False,
    ) -> dict[str, Any]:
        """Parse a PDF document (bytes or path), without changing storage."""
        try:
            if not is_path and not file_data.startswith(b"%PDF"):
                logging.warning("Invalid PDF format for %s", filename)
                return {"error": "Invalid PDF file format"}

            logging.info("Processing PDF %s with pypdf", filename)

            sync_input = file_data if is_path else io.BytesIO(file_data)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._process_pdf_sync, sync_input, self.max_pages)

            if "error" in result:
                return result

            full_text = result["content"]
            pages_count = result["pages"]

            return {
                "success": True,
                "filename": filename,
                "pages": pages_count,
                "text_length": len(full_text),
                "content": full_text,
                "method": "pypdf",
                "truncated": result.get("truncated", False),
            }

        except (ValueError, OSError, pypdf.errors.PdfReadError) as e:
            logging.error("Error processing PDF %s: %s", filename, e, exc_info=True)
            await metrics_collector.record_error("pdf_processing", str(e))
            return {"error": f"Error processing PDF: {str(e)}"}

    # ── Word parsing ─────────────────────────────────────────────────────────

    async def _process_word_unified(
        self,
        file_data,
        filename: str,
        is_path: bool = False,
    ) -> dict[str, Any]:
        """Parse a Word document (bytes or path), without changing storage."""
        if not is_path and not file_data.startswith(b"\x50\x4b\x03\x04"):
            logging.warning("Invalid DOCX format for %s: Missing ZIP header", filename)
            return {"error": "Invalid Word document format. File must be a valid .docx file."}

        try:
            sync_input = file_data if is_path else io.BytesIO(file_data)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._process_word_sync, sync_input)

            if "error" in result:
                logging.error("Error processing Word document %s: %s", filename, result["error"])
                return {"error": f"Error processing Word document: {result['error']}"}

            result["filename"] = filename
            return result

        except (ValueError, UnicodeDecodeError, OSError) as e:
            logging.error("Error processing Word document %s: %s", filename, e, exc_info=True)
            await metrics_collector.record_error("word_processing", str(e))
            return {"error": f"Error processing Word document: {str(e)}"}

    # ── Repository methods used by handlers and scheduled maintenance ──────

    async def get_document_by_id(self, document_id, user_id):
        from app.documents.repository import get_document_by_id

        return await get_document_by_id(document_id, user_id)

    async def get_user_documents(self, user_id):
        from app.documents.repository import get_user_documents

        return await get_user_documents(user_id)

    async def get_document_content(self, document_id, user_id):
        from app.documents.repository import get_document_content

        return await get_document_content(document_id, user_id)

    async def delete_document(self, document_id, user_id):
        from app.documents.repository import delete_document

        return await delete_document(document_id, user_id)

    async def delete_all_user_documents(self, user_id):
        from app.documents.repository import delete_all_user_documents

        return await delete_all_user_documents(user_id)

    async def cleanup_old_documents(self, days_old=3):
        from app.documents.repository import cleanup_old_documents

        return await cleanup_old_documents(days_old)

    async def get_document_stats(self):
        from app.documents.repository import get_document_stats

        return await get_document_stats()

    async def get_user_document_stats(self, user_id):
        from app.documents.repository import get_user_document_stats

        return await get_user_document_stats(user_id)


# Singleton
document_processor = DocumentProcessor()


# ============================================================================
# FACADE FUNCTIONS
# ============================================================================


async def process_uploaded_document(
    file_data, filename: str, user_id: int, is_path: bool = False, *, skip_duplicate_check: bool = False
) -> dict[str, Any]:
    """Process an uploaded document."""
    return await document_processor.process_document(
        file_data, filename, user_id, is_path, skip_duplicate_check=skip_duplicate_check
    )


async def get_user_documents(user_id: int) -> list[dict[str, Any]]:
    """Get user's documents."""
    return await document_processor.get_user_documents(user_id)


async def get_document_content(document_id: int, user_id: int) -> str | None:
    """Get document content."""
    return await document_processor.get_document_content(document_id, user_id)


async def delete_user_document(document_id: int, user_id: int) -> bool:
    """Delete a user document."""
    return await document_processor.delete_document(document_id, user_id)


async def delete_all_user_documents(user_id: int) -> int:
    """Delete all user documents."""
    return await document_processor.delete_all_user_documents(user_id)


async def get_document_by_id(document_id: int, user_id: int) -> dict[str, Any] | None:
    """Get document by ID."""
    return await document_processor.get_document_by_id(document_id, user_id)
