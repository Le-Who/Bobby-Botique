import logging
import hashlib
import tempfile
import asyncio
import asyncpg
import io
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
import httpx
import pypdf
from docx import Document
# PyMuPDF removed for free tier optimization

from app.config import settings
from app import database
from app.repos.users import is_admin
from app.utils.network import NetworkErrorHandler
from app.metrics import metrics_collector

# Maximum characters to extract from a document to prevent OOM and performance issues
MAX_DOCUMENT_TEXT_LENGTH = 100000

# Check поддержку documentов
try:
    # PyMuPDF removed for free tier optimization
    DOCUMENT_SUPPORT = True
except ImportError:
    DOCUMENT_SUPPORT = False
    logging.warning(
        "Document processing libraries not installed. Document support disabled."
    )


class DocumentProcessor:
    """Процессор для обработки документов"""

    def __init__(self):
        self.supported_formats = [".pdf", ".docx", ".doc"]
        # Optimized for free tier resource constraints
        self.max_file_size = 10 * 1024 * 1024  # 10MB for free tier
        self.max_pages = 50  # Reduced page limit for performance

    def _write_temp_file_sync(self, file_data: bytes, suffix: str) -> str:
        """Synchronously write data to a temp file and return path.
        This should be run in a separate thread to avoid blocking the event loop.
        """
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(file_data)
            return temp_file.name

    @staticmethod
    def _calculate_file_hash_sync(file_path_or_data: Union[str, bytes]) -> str:
        """Вычисляет SHA-256 хэш файла (потоково, если это путь)"""
        if isinstance(file_path_or_data, bytes):
            return hashlib.sha256(file_path_or_data).hexdigest()

        h = hashlib.sha256()
        with open(file_path_or_data, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _check_duplicate_file(
        self, user_id: int, file_hash: str, filename: str
    ) -> Optional[Dict[str, Any]]:
        """Проверяет, есть ли уже такой файл у пользователя"""
        try:
            result = await database.db_query(
                "SELECT id, filename, created_at FROM user_documents WHERE user_id = $1 AND file_hash = $2",
                (user_id, file_hash),
            )
            if result:
                return {
                    "id": result[0]["id"],
                    "filename": result[0]["filename"],
                    "created_at": result[0]["created_at"],
                }
            return None
        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error checking duplicate file: %s", e, exc_info=True)
            return None

    async def _check_document_limit(self, user_id: int) -> bool:
        """Проверяет, не превышен ли лимит документов для пользователя"""
        try:
            result = await database.db_query(
                "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
                (user_id,),
            )
            doc_count = result[0]["doc_count"] if result else 0
            return doc_count < settings.MAX_DOCUMENTS_PER_USER
        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error checking document limit: %s", e, exc_info=True)
            return True  # В случае ошибки разрешаем загрузку

    async def _cleanup_oldest_documents(self, user_id: int, keep_count: int = 4) -> int:
        """Удаляет старые документы пользователя, оставляя указанное количество"""
        try:
            # Оптимfromировано: удаляем old documents одним requestом с подrequestом
            result = await database.db_query(
                """
                DELETE FROM user_documents
                WHERE id IN (
                    SELECT id FROM user_documents
                    WHERE user_id = $1
                    ORDER BY created_at ASC
                    OFFSET $2
                )
                RETURNING id
            """,
                (user_id, keep_count),
            )

            if not result:
                return 0

            deleted_count = len(result)
            logging.info(
                f"Cleaned up {deleted_count} oldest documents for user {user_id}"
            )
            return deleted_count

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error cleaning up oldest documents: %s", e, exc_info=True)
            return 0

    async def process_document(
        self, file_data, filename: str, user_id: int, is_path: bool = False
    ) -> Dict[str, Any]:
        """Обрабатывает документ и возвращает извлеченный текст. file_data может быть путем (str) или bytes"""
        if not DOCUMENT_SUPPORT:
            return {"error": "Document processing is not available"}

        try:
            # Check размер fileа
            if is_path:
                import os

                file_size = os.path.getsize(file_data)
            else:
                file_size = len(file_data)

            if file_size > self.max_file_size:
                return {
                    "error": f"File too large. Maximum size is {self.max_file_size // (1024 * 1024)}MB"
                }

            # Определяем тип fileа
            file_ext = Path(filename).suffix.lower()
            if file_ext not in self.supported_formats:
                return {"error": f"Unsupported file format: {file_ext}"}

            # Check limit documentов
            if not await self._check_document_limit(user_id):
                # If limit превышен, удаляем самый old document
                await self._cleanup_oldest_documents(user_id, 4)
                logging.info(
                    f"Document limit exceeded for user {user_id}, removed oldest document"
                )

            # Вычисляем хэш fileа и проверяем дубликаты
            # Offload hash calculation to executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            file_hash = await loop.run_in_executor(
                None, self._calculate_file_hash_sync, file_data
            )
            duplicate = await self._check_duplicate_file(user_id, file_hash, filename)

            if duplicate:
                # Правильно обрабатываем datetime
                created_date = duplicate["created_at"]
                if hasattr(created_date, "strftime"):
                    # Это объект datetime
                    date_str = created_date.strftime("%Y-%m-%d")
                else:
                    # Это строка
                    date_str = str(created_date)[:10]

                return {
                    "error": "duplicate",
                    "message": f"Файл '{filename}' уже был загружен ранее как '{duplicate['filename']}' ({date_str})",
                    "duplicate_info": duplicate,
                }

            # Process document
            if file_ext == ".pdf":
                return await self._process_pdf_unified(
                    file_data, filename, user_id, file_hash, is_path=is_path
                )
            elif file_ext in [".docx", ".doc"]:
                return await self._process_word_unified(
                    file_data, filename, user_id, file_hash, is_path=is_path
                )
            else:
                return {"error": f"Unsupported file format: {file_ext}"}

        except (ValueError, UnicodeDecodeError, OSError) as e:
            logging.error("Error processing document %s: %s", filename, e, exc_info=True)
            await metrics_collector.record_error("document_processing", str(e))
            return {"error": f"Error processing document: {str(e)}"}

    async def process_document_force(
        self, file_data, filename: str, user_id: int, is_path: bool = False
    ) -> Dict[str, Any]:
        """Обрабатывает документ принудительно (игнорируя дубликаты)"""
        if not DOCUMENT_SUPPORT:
            return {"error": "Document processing is not available"}

        try:
            # Check размер fileа
            if is_path:
                import os

                file_size = os.path.getsize(file_data)
            else:
                file_size = len(file_data)

            if file_size > self.max_file_size:
                return {
                    "error": f"File too large. Maximum size is {self.max_file_size // (1024 * 1024)}MB"
                }

            # Определяем тип fileа
            file_ext = Path(filename).suffix.lower()
            if file_ext not in self.supported_formats:
                return {"error": f"Unsupported file format: {file_ext}"}

            # Вычисляем хэш fileа (но не проверяем дубликаты)
            # Offload hash calculation to executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            file_hash = await loop.run_in_executor(
                None, self._calculate_file_hash_sync, file_data
            )

            # Process document
            if file_ext == ".pdf":
                return await self._process_pdf_unified(
                    file_data, filename, user_id, file_hash, is_path=is_path
                )
            elif file_ext in [".docx", ".doc"]:
                return await self._process_word_unified(
                    file_data, filename, user_id, file_hash, is_path=is_path
                )
            else:
                return {"error": f"Unsupported file format: {file_ext}"}

        except (ValueError, UnicodeDecodeError, OSError) as e:
            logging.error("Error processing document %s: %s", filename, e, exc_info=True)
            await metrics_collector.record_error("document_processing", str(e))
            return {"error": f"Error processing document: {str(e)}"}

    async def _process_pdf_unified(
        self, file_data, filename: str, user_id: int, file_hash: str,
        is_path: bool = False
    ) -> Dict[str, Any]:
        """Обрабатывает PDF документ (bytes или path)."""
        try:
            if not is_path:
                # Validate magic bytes
                if not file_data.startswith(b"%PDF"):
                    logging.warning("Invalid PDF format for %s", filename)
                    return {"error": "Invalid PDF file format"}

            logging.info("Processing PDF %s with PyPDF2", filename)

            # Prepare input for sync function
            if is_path:
                sync_input = file_data  # str path
            else:
                import tempfile
                stream = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")
                stream.write(file_data)
                stream.seek(0)
                sync_input = stream

            # Run CPU-bound task in executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, self._process_pdf_sync, sync_input, self.max_pages
            )

            if "error" in result:
                return result

            full_text = result["content"]
            pages_count = result["pages"]

            # Save в базу данных
            await self._save_document_content(
                user_id, filename, full_text, pages_count, file_hash
            )

            return {
                "success": True,
                "filename": filename,
                "pages": pages_count,
                "text_length": len(full_text),
                "content": full_text,
                "method": "PyPDF2",
            }

        except (ValueError, OSError, pypdf.errors.PdfReadError) as e:
            logging.error("Error processing PDF %s: %s", filename, e, exc_info=True)
            await metrics_collector.record_error("pdf_processing", str(e))
            return {"error": f"Error processing PDF: {str(e)}"}

    async def _process_word_unified(
        self, file_data, filename: str, user_id: int, file_hash: str,
        is_path: bool = False
    ) -> Dict[str, Any]:
        """Обрабатывает Word документ (bytes или path)."""
        if not is_path:
            # Validate magic bytes for ZIP (all .docx files are ZIPs)
            if not file_data.startswith(b"\x50\x4b\x03\x04"):
                logging.warning("Invalid DOCX format for %s: Missing ZIP header", filename)
                return {
                    "error": "Invalid Word document format. File must be a valid .docx file."
                }

        try:
            if is_path:
                sync_input = file_data  # str path
            else:
                import tempfile
                stream = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")
                stream.write(file_data)
                stream.seek(0)
                sync_input = stream

            # Offload CPU-bound task to executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._process_word_sync, sync_input)

            if "error" in result:
                logging.error(
                    f"Error processing Word document {filename}: {result['error']}"
                )
                return {"error": f"Error processing Word document: {result['error']}"}

            full_text = result["content"]

            # Save в базу данных
            await self._save_document_content(
                user_id, filename, full_text, 1, file_hash
            )  # Word documents count как 1 страницу

            result["filename"] = filename
            return result

        except (ValueError, UnicodeDecodeError, OSError) as e:
            logging.error(
                f"Error processing Word document {filename}: {e}", exc_info=True
            )
            await metrics_collector.record_error("word_processing", str(e))
            return {"error": f"Error processing Word document: {str(e)}"}

    async def _save_document_content(
        self, user_id: int, filename: str, content: str, pages: int, file_hash: str
    ):
        """Сохраняет содержимое документа в базу данных"""
        try:
            # The table is created in database.py

            # NOTE: Schema migrations are now centralized in database.py

            # Save document
            await database.db_query(
                "INSERT INTO user_documents (user_id, filename, content, pages, file_size, file_hash) VALUES ($1, $2, $3, $4, $5, $6)",
                (user_id, filename, content, pages, len(content), file_hash),
            )

            logging.info("Saved document %s for user %s", filename, user_id)

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error saving document to database: %s", e, exc_info=True)

    async def get_document_by_id(
        self, document_id: int, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Получает документ по ID"""
        try:
            result = await database.db_query(
                "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE id = $1 AND user_id = $2",
                (document_id, user_id),
            )

            if result:
                row = result[0]
                return {
                    "id": row["id"],
                    "filename": row["filename"],
                    "pages": row["pages"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                    "file_size": row["file_size"],
                    "file_hash": row["file_hash"],
                }
            return None

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error getting document by ID: %s", e, exc_info=True)
            return None

    async def get_user_documents(self, user_id: int) -> List[Dict[str, Any]]:
        """Получает список документов пользователя"""
        try:
            # Устанавливаем context user for RLS
            await database.set_user_context(user_id, is_admin(user_id))

            try:
                result = await database.db_query(
                    "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC",
                    (user_id,),
                )

                return [
                    {
                        "id": row["id"],
                        "filename": row["filename"],
                        "pages": row["pages"],
                        "created_at": row["created_at"].isoformat()
                        if row["created_at"]
                        else None,
                        "file_size": row["file_size"],
                        "file_hash": row["file_hash"],
                    }
                    for row in result
                ]
            finally:
                # Clean up context user
                await database.clear_user_context()

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error getting user documents: %s", e, exc_info=True)
            return []

    async def get_document_content(
        self, document_id: int, user_id: int
    ) -> Optional[str]:
        """Получает содержимое документа"""
        try:
            # Устанавливаем context user for RLS
            await database.set_user_context(user_id, is_admin(user_id))

            try:
                result = await database.db_query(
                    "SELECT content FROM user_documents WHERE id = $1 AND user_id = $2",
                    (document_id, user_id),
                )

                if result:
                    return result[0]["content"]
                return None
            finally:
                # Clean up context user
                await database.clear_user_context()

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error getting document content: %s", e, exc_info=True)
            return None

    async def delete_document(self, document_id: int, user_id: int) -> bool:
        """Удаляет документ"""
        try:
            await database.db_query(
                "DELETE FROM user_documents WHERE id = $1 AND user_id = $2",
                (document_id, user_id),
            )
            return True

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error deleting document: %s", e, exc_info=True)
            return False

    async def delete_all_user_documents(self, user_id: int) -> int:
        """Удаляет все документы пользователя"""
        try:
            # Возвращает количество удаленных записей
            result = await database.db_query(
                "DELETE FROM user_documents WHERE user_id = $1 RETURNING id",
                (user_id,),
            )
            return len(result) if result else 0

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error deleting all documents: %s", e, exc_info=True)
            return 0

    async def cleanup_old_documents(self, days_old: int = 3) -> int:
        """Очищает документы старше указанного количества дней"""
        try:
            result = await database.db_query(
                """
                DELETE FROM user_documents
                WHERE created_at < (CURRENT_TIMESTAMP - ($1 * INTERVAL '1 day'))
            """,
                (days_old,),
            )

            # DELETE queries return the number of affected rows
            deleted_count = len(result) if result else 0
            logging.info(
                f"Cleaned up {deleted_count} old documents (older than {days_old} days)"
            )
            return deleted_count

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error cleaning up old documents: %s", e, exc_info=True)
            return 0

    async def get_document_stats(self) -> Dict[str, Any]:
        """Получает статистику документов"""
        try:
            # Размер БД (onблfromительно)
            size_result = await database.db_query("""
                SELECT 
                    COUNT(*) as doc_count,
                    COALESCE(SUM(file_size), 0) as total_size,
                    COALESCE(AVG(file_size), 0) as avg_size
                FROM user_documents
            """)

            if size_result:
                stats = size_result[0]
                return {
                    "total_documents": stats["doc_count"],
                    "total_size_chars": stats["total_size"],
                    "average_size_chars": stats["avg_size"],
                    "total_size_mb": stats["total_size"] / (1024 * 1024)
                    if stats["total_size"]
                    else 0,
                }

            return {
                "total_documents": 0,
                "total_size_chars": 0,
                "average_size_chars": 0,
                "total_size_mb": 0,
            }

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error getting document stats: %s", e, exc_info=True)
            return {
                "total_documents": 0,
                "total_size_chars": 0,
                "average_size_chars": 0,
                "total_size_mb": 0,
            }

    async def get_user_document_stats(self, user_id: int) -> Dict[str, Any]:
        """Получает статистику документов конкретного пользователя"""
        try:
            # Количество documentов user
            count_result = await database.db_query(
                "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
                (user_id,),
            )
            doc_count = count_result[0]["doc_count"] if count_result else 0

            # Размер documentов user
            size_result = await database.db_query(
                """
                SELECT 
                    COALESCE(SUM(file_size), 0) as total_size,
                    COALESCE(AVG(file_size), 0) as avg_size
                FROM user_documents 
                WHERE user_id = $1
            """,
                (user_id,),
            )

            if size_result:
                stats = size_result[0]
                return {
                    "document_count": doc_count,
                    "total_size_chars": stats["total_size"],
                    "average_size_chars": stats["avg_size"],
                    "total_size_mb": stats["total_size"] / (1024 * 1024)
                    if stats["total_size"]
                    else 0,
                    "limit_reached": doc_count >= 5,
                    "can_upload": doc_count < 5,
                }

            return {
                "document_count": 0,
                "total_size_chars": 0,
                "average_size_chars": 0,
                "total_size_mb": 0,
                "limit_reached": False,
                "can_upload": True,
            }

        except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            logging.error("Error getting user document stats: %s", e, exc_info=True)
            return {
                "document_count": 0,
                "total_size_chars": 0,
                "average_size_chars": 0,
                "total_size_mb": 0,
                "limit_reached": False,
                "can_upload": True,
            }


# Глобальный экземпляр процессора documentов
document_processor = DocumentProcessor()


async def process_uploaded_document(
    file_data, filename: str, user_id: int, is_path: bool = False
) -> Dict[str, Any]:
    """Обрабатывает загруженный документ"""
    return await document_processor.process_document(
        file_data, filename, user_id, is_path
    )


async def process_uploaded_document_force(
    file_data, filename: str, user_id: int, is_path: bool = False
) -> Dict[str, Any]:
    """Обрабатывает загруженный документ принудительно (игнорируя дубликаты)"""
    return await document_processor.process_document_force(
        file_data, filename, user_id, is_path
    )


async def get_user_documents(user_id: int) -> List[Dict[str, Any]]:
    """Получает документы пользователя"""
    return await document_processor.get_user_documents(user_id)


async def get_document_content(document_id: int, user_id: int) -> Optional[str]:
    """Получает содержимое документа"""
    return await document_processor.get_document_content(document_id, user_id)


async def delete_user_document(document_id: int, user_id: int) -> bool:
    """Удаляет документ пользователя"""
    return await document_processor.delete_document(document_id, user_id)


async def delete_all_user_documents(user_id: int) -> int:
    """Удаляет все документы пользователя"""
    return await document_processor.delete_all_user_documents(user_id)


async def _upload_file_to_x0_at(file_data: bytes, filename: str) -> Optional[str]:
    """Internal function for uploading file to x0.at with retry logic."""
    timeout_config = httpx.Timeout(
        connect=10.0,  # 10 секунд на подkeyение
        read=60.0,  # 60 секунд на чтение (for загрузки fileов)
        write=60.0,  # 60 секунд на запись (for загрузки fileов)
        pool=30.0,  # 30 секунд на получение соединения from пула
    )

    async with httpx.AsyncClient(timeout=timeout_config) as client:
        files = {"file": (filename, file_data)}
        response = await client.post("https://x0.at/", files=files)

        if response.status_code == 200:
            url = response.text.strip()
            if url.startswith("http"):
                logging.info("File %s uploaded to x0.at: %s", filename, url)
                return url
            else:
                logging.error("Invalid response from x0.at: %s", response.text)
                return None
        else:
            logging.error(
                f"Failed to upload to x0.at: {response.status_code} - {response.text}"
            )
            return None


async def upload_to_x0_at(file_data: bytes, filename: str) -> Optional[str]:
    """Загружает файл на внешний сервис x0.at и возвращает URL с автоматическими повторами"""
    try:
        return await NetworkErrorHandler.retry_with_backoff(
            _upload_file_to_x0_at,
            max_retries=3,
            base_delay=2.0,
            file_data=file_data,
            filename=filename,
        )
    except (httpx.HTTPError, OSError) as e:
        logging.error("Error uploading to x0.at after retries: %s", e, exc_info=True)
        return None


async def get_document_by_id(
    document_id: int, user_id: int
) -> Optional[Dict[str, Any]]:
    """Получает документ по ID"""
    return await document_processor.get_document_by_id(document_id, user_id)
