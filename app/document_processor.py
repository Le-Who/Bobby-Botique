import logging
import hashlib
import tempfile
import asyncio
import io
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from datetime import datetime
import httpx
import pypdf
from docx import Document
# PyMuPDF removed for free tier optimization

from app.config import settings
from app import database
from app.utils.network import NetworkErrorHandler
from app.metrics import metrics_collector

# Maximum characters to extract from a document to prevent OOM and performance issues
MAX_DOCUMENT_TEXT_LENGTH = 100000

# Проверяем поддержку документов
try:
    # PyMuPDF removed for free tier optimization
    DOCUMENT_SUPPORT = True
except ImportError:
    DOCUMENT_SUPPORT = False
    logging.warning("Document processing libraries not installed. Document support disabled.")

class DocumentProcessor:
    """Процессор для обработки документов"""
    
    def __init__(self):
        self.supported_formats = ['.pdf', '.docx', '.doc']
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
    def _calculate_file_hash_sync(file_data: bytes) -> str:
        """Вычисляет SHA-256 хэш файла"""
        return hashlib.sha256(file_data).hexdigest()
    
    async def _check_duplicate_file(self, user_id: int, file_hash: str, filename: str) -> Optional[Dict[str, Any]]:
        """Проверяет, есть ли уже такой файл у пользователя"""
        try:
            result = await database.db_query(
                "SELECT id, filename, created_at FROM user_documents WHERE user_id = $1 AND file_hash = $2",
                (user_id, file_hash)
            )
            if result:
                return {
                    'id': result[0]['id'],
                    'filename': result[0]['filename'],
                    'created_at': result[0]['created_at']
                }
            return None
        except Exception as e:
            logging.error(f"Error checking duplicate file: {e}")
            return None
    
    async def _check_document_limit(self, user_id: int) -> bool:
        """Проверяет, не превышен ли лимит документов для пользователя"""
        try:
            result = await database.db_query(
                "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
                (user_id,)
            )
            doc_count = result[0]['doc_count'] if result else 0
            return doc_count < settings.MAX_DOCUMENTS_PER_USER
        except Exception as e:
            logging.error(f"Error checking document limit: {e}")
            return True  # В случае ошибки разрешаем загрузку
    
    async def _cleanup_oldest_documents(self, user_id: int, keep_count: int = 4) -> int:
        """Удаляет старые документы пользователя, оставляя указанное количество"""
        try:
            # Оптимизировано: удаляем старые документы одним запросом с подзапросом
            result = await database.db_query("""
                DELETE FROM user_documents
                WHERE id IN (
                    SELECT id FROM user_documents
                    WHERE user_id = $1
                    ORDER BY created_at ASC
                    OFFSET $2
                )
                RETURNING id
            """, (user_id, keep_count))
            
            if not result:
                return 0
                
            deleted_count = len(result)
            logging.info(f"Cleaned up {deleted_count} oldest documents for user {user_id}")
            return deleted_count
            
        except Exception as e:
            logging.error(f"Error cleaning up oldest documents: {e}")
            return 0
    
    async def process_document(self, file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
        """Обрабатывает документ и возвращает извлеченный текст"""
        if not DOCUMENT_SUPPORT:
            return {"error": "Document processing is not available"}
        
        try:
            # Проверяем размер файла
            if len(file_data) > self.max_file_size:
                return {"error": f"File too large. Maximum size is {self.max_file_size // (1024*1024)}MB"}
            
            # Определяем тип файла
            file_ext = Path(filename).suffix.lower()
            if file_ext not in self.supported_formats:
                return {"error": f"Unsupported file format: {file_ext}"}
            
            # Проверяем лимит документов
            if not await self._check_document_limit(user_id):
                # Если лимит превышен, удаляем самый старый документ
                await self._cleanup_oldest_documents(user_id, 4)
                logging.info(f"Document limit exceeded for user {user_id}, removed oldest document")
            
            # Вычисляем хэш файла и проверяем дубликаты
            # Offload hash calculation to executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            file_hash = await loop.run_in_executor(None, self._calculate_file_hash_sync, file_data)
            duplicate = await self._check_duplicate_file(user_id, file_hash, filename)
            
            if duplicate:
                # Правильно обрабатываем datetime
                created_date = duplicate['created_at']
                if hasattr(created_date, 'strftime'):
                    # Это объект datetime
                    date_str = created_date.strftime('%Y-%m-%d')
                else:
                    # Это строка
                    date_str = str(created_date)[:10]
                
                return {
                    "error": "duplicate",
                    "message": f"Файл '{filename}' уже был загружен ранее как '{duplicate['filename']}' ({date_str})",
                    "duplicate_info": duplicate
                }
            
            # Обрабатываем документ
            if file_ext == '.pdf':
                return await self._process_pdf(file_data, filename, user_id, file_hash)
            elif file_ext in ['.docx', '.doc']:
                return await self._process_word(file_data, filename, user_id, file_hash)
            else:
                return {"error": f"Unsupported file format: {file_ext}"}
                
        except Exception as e:
            logging.error(f"Error processing document {filename}: {e}")
            await metrics_collector.record_error("document_processing", str(e))
            return {"error": f"Error processing document: {str(e)}"}
    
    async def process_document_force(self, file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
        """Обрабатывает документ принудительно (игнорируя дубликаты)"""
        if not DOCUMENT_SUPPORT:
            return {"error": "Document processing is not available"}
        
        try:
            # Проверяем размер файла
            if len(file_data) > self.max_file_size:
                return {"error": f"File too large. Maximum size is {self.max_file_size // (1024*1024)}MB"}
            
            # Определяем тип файла
            file_ext = Path(filename).suffix.lower()
            if file_ext not in self.supported_formats:
                return {"error": f"Unsupported file format: {file_ext}"}
            
            # Вычисляем хэш файла (но не проверяем дубликаты)
            # Offload hash calculation to executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            file_hash = await loop.run_in_executor(None, self._calculate_file_hash_sync, file_data)
            
            # Обрабатываем документ
            if file_ext == '.pdf':
                return await self._process_pdf(file_data, filename, user_id, file_hash)
            elif file_ext in ['.docx', '.doc']:
                return await self._process_word(file_data, filename, user_id, file_hash)
            else:
                return {"error": f"Unsupported file format: {file_ext}"}
                
        except Exception as e:
            logging.error(f"Error processing document {filename}: {e}")
            await metrics_collector.record_error("document_processing", str(e))
            return {"error": f"Error processing document: {str(e)}"}
    
    async def _process_pdf(self, file_data: bytes, filename: str, user_id: int, file_hash: str) -> Dict[str, Any]:
        """Обрабатывает PDF документ"""
        try:
            # Проверяем, что файл является корректным PDF до создания временного файла
            if not file_data.startswith(b'%PDF'):
                logging.warning(f"Invalid PDF format for {filename}")
                return {"error": "Invalid PDF file format"}

            logging.info(f"Processing PDF {filename} with PyPDF2")
            
            # PyMuPDF removed for free tier optimization, using PyPDF2 directly
            return await self._process_pdf_with_pypdf2(file_data, filename, user_id, file_hash)
            
        except Exception as e:
            logging.error(f"Error processing PDF {filename}: {e}", exc_info=True)
            await metrics_collector.record_error("pdf_processing", str(e))
            return {"error": f"Error processing PDF: {str(e)}"}

    
    @staticmethod
    def _process_pdf_sync(input_data: Union[str, io.BytesIO], max_pages: int) -> Dict[str, Any]:
        """Synchronous part of PDF processing to run in executor"""
        pdf_file = None
        should_close = False

        try:
            if isinstance(input_data, str):
                pdf_file = open(input_data, 'rb')
                should_close = True
                stream = pdf_file
            else:
                stream = input_data

            pdf_reader = pypdf.PdfReader(stream)

            if len(pdf_reader.pages) > max_pages:
                return {"error": f"PDF too large. Maximum {max_pages} pages allowed"}

            text_content = []
            current_length = 0

            for page_num, page in enumerate(pdf_reader.pages):
                try:
                    text = page.extract_text()
                    if text.strip():
                        chunk = f"--- Page {page_num + 1} ---\n{text}"
                        text_content.append(chunk)
                        current_length += len(chunk)
                except Exception as page_error:
                    logging.warning(f"Error extracting text from page {page_num + 1}: {page_error}")
                    chunk = f"--- Page {page_num + 1} ---\n[Error extracting text from this page]"
                    text_content.append(chunk)
                    current_length += len(chunk)

                # Проверяем лимит токенов
                if current_length > MAX_DOCUMENT_TEXT_LENGTH:
                    text_content.append(f"\n--- Document truncated at page {page_num + 1} ---")
                    break

            full_text = '\n\n'.join(text_content)

            return {
                "success": True,
                "pages": len(pdf_reader.pages),
                "content": full_text
            }
        except Exception as e:
            return {"error": str(e)}
        finally:
            if should_close and pdf_file:
                pdf_file.close()

    async def _process_pdf_with_pypdf2(self, file_data: bytes, filename: str, user_id: int, file_hash: str) -> Dict[str, Any]:
        """Обрабатывает PDF документ с использованием PyPDF2 (fallback)"""
        try:
            # Use io.BytesIO to avoid writing to disk
            stream = io.BytesIO(file_data)
            
            # Run CPU-bound task in executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                self._process_pdf_sync,
                stream,
                self.max_pages
            )

            if "error" in result:
                return result

            full_text = result["content"]
            pages_count = result["pages"]

            # Сохраняем в базу данных
            await self._save_document_content(user_id, filename, full_text, pages_count, file_hash)

            return {
                "success": True,
                "filename": filename,
                "pages": pages_count,
                "text_length": len(full_text),
                "content": full_text,
                "method": "PyPDF2"
            }
                    
        except Exception as e:
            logging.error(f"Error processing PDF with PyPDF2 {filename}: {e}", exc_info=True)
            await metrics_collector.record_error("pdf_processing_pypdf2", str(e))
            return {"error": f"Error processing PDF with PyPDF2: {str(e)}"}

    @staticmethod
    def _process_word_sync(input_data: Union[str, io.BytesIO]) -> Dict[str, Any]:
        """Synchronous part of Word processing to run in executor"""
        try:
            doc = Document(input_data)

            text_content = []
            paragraph_count = 0
            current_length = 0

            # Извлекаем текст из параграфов
            for para in doc.paragraphs:
                if para.text.strip():
                    text = para.text
                    text_content.append(text)
                    paragraph_count += 1
                    current_length += len(text)

                    if current_length > MAX_DOCUMENT_TEXT_LENGTH:
                        text_content.append(f"\n--- Document truncated at {MAX_DOCUMENT_TEXT_LENGTH} chars ---")
                        break

            # Only process tables if we haven't hit the limit
            if current_length <= MAX_DOCUMENT_TEXT_LENGTH:
                # Извлекаем текст из таблиц
                table_count = 0
                for table in doc.tables:
                    table_count += 1
                    text_content.append(f"\n--- Table {table_count} ---")
                    # Approximate length increment for table header
                    current_length += len(text_content[-1])

                    for row in table.rows:
                        row_text = []
                        for cell in row.cells:
                            if cell.text.strip():
                                row_text.append(cell.text.strip())
                        if row_text:
                            line = " | ".join(row_text)
                            text_content.append(line)
                            current_length += len(line)

                            if current_length > MAX_DOCUMENT_TEXT_LENGTH:
                                break

                    if current_length > MAX_DOCUMENT_TEXT_LENGTH:
                        text_content.append(f"\n--- Document truncated at {MAX_DOCUMENT_TEXT_LENGTH} chars ---")
                        break

            full_text = '\n\n'.join(text_content)

            return {
                "success": True,
                "pages": 1,
                "paragraphs": paragraph_count,
                "tables": len(doc.tables),
                "text_length": len(full_text),
                "content": full_text
            }
        except Exception as e:
            return {"error": str(e)}
    
    async def _process_word(self, file_data: bytes, filename: str, user_id: int, file_hash: str) -> Dict[str, Any]:
        """Обрабатывает Word документ"""
        # Validate magic bytes for ZIP (all .docx files are ZIPs)
        # PK\x03\x04
        if not file_data.startswith(b'\x50\x4b\x03\x04'):
            logging.warning(f"Invalid DOCX format for {filename}: Missing ZIP header")
            return {"error": "Invalid Word document format. File must be a valid .docx file."}

        try:
            # Use io.BytesIO to avoid writing to disk
            stream = io.BytesIO(file_data)
            
            # Offload CPU-bound task to executor
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                self._process_word_sync,
                stream
            )

            if "error" in result:
                logging.error(f"Error processing Word document {filename}: {result['error']}")
                return {"error": f"Error processing Word document: {result['error']}"}

            full_text = result["content"]

            # Сохраняем в базу данных
            await self._save_document_content(user_id, filename, full_text, 1, file_hash)  # Word документы считаем как 1 страницу

            # Merge filename into result as it's not in sync output
            result["filename"] = filename
            return result
                
        except Exception as e:
            logging.error(f"Error processing Word document {filename}: {e}", exc_info=True)
            await metrics_collector.record_error("word_processing", str(e))
            return {"error": f"Error processing Word document: {str(e)}"}
    
    async def _save_document_content(self, user_id: int, filename: str, content: str, pages: int, file_hash: str):
        """Сохраняет содержимое документа в базу данных"""
        try:
            # The table is created in database.py
            
            # NOTE: Schema migrations are now centralized in database.py
            
                         # Сохраняем документ
            await database.db_query(
                "INSERT INTO user_documents (user_id, filename, content, pages, file_size, file_hash) VALUES ($1, $2, $3, $4, $5, $6)",
                (user_id, filename, content, pages, len(content), file_hash)
            )
            
            logging.info(f"Saved document {filename} for user {user_id}")
            
        except Exception as e:
            logging.error(f"Error saving document to database: {e}")
    
    async def get_document_by_id(self, document_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """Получает документ по ID"""
        try:
            result = await database.db_query(
                "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE id = $1 AND user_id = $2",
                (document_id, user_id)
            )
            
            if result:
                row = result[0]
                return {
                    'id': row['id'],
                    'filename': row['filename'],
                    'pages': row['pages'],
                    'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                    'file_size': row['file_size'],
                    'file_hash': row['file_hash']
                }
            return None
            
        except Exception as e:
            logging.error(f"Error getting document by ID: {e}")
            return None

    async def get_user_documents(self, user_id: int) -> List[Dict[str, Any]]:
        """Получает список документов пользователя"""
        try:
            # Устанавливаем контекст пользователя для RLS
            await database.set_user_context(user_id, database.is_admin(user_id))
            
            try:
                result = await database.db_query(
                    "SELECT id, filename, pages, created_at, file_size, file_hash FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC",
                    (user_id,)
                )
                
                return [
                    {
                        'id': row['id'],
                        'filename': row['filename'],
                        'pages': row['pages'],
                        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                        'file_size': row['file_size'],
                        'file_hash': row['file_hash']
                    }
                    for row in result
                ]
            finally:
                # Очищаем контекст пользователя
                await database.clear_user_context()
            
        except Exception as e:
            logging.error(f"Error getting user documents: {e}")
            return []
    
    async def get_document_content(self, document_id: int, user_id: int) -> Optional[str]:
        """Получает содержимое документа"""
        try:
            # Устанавливаем контекст пользователя для RLS
            await database.set_user_context(user_id, database.is_admin(user_id))
            
            try:
                result = await database.db_query(
                    "SELECT content FROM user_documents WHERE id = $1 AND user_id = $2",
                    (document_id, user_id)
                )
                
                if result:
                    return result[0]['content']
                return None
            finally:
                # Очищаем контекст пользователя
                await database.clear_user_context()
            
        except Exception as e:
            logging.error(f"Error getting document content: {e}")
            return None
    
    async def delete_document(self, document_id: int, user_id: int) -> bool:
        """Удаляет документ"""
        try:
            await database.db_query(
                "DELETE FROM user_documents WHERE id = $1 AND user_id = $2",
                (document_id, user_id)
            )
            return True
            
        except Exception as e:
            logging.error(f"Error deleting document: {e}")
            return False

    async def cleanup_old_documents(self, days_old: int = 3) -> int:
        """Очищает документы старше указанного количества дней"""
        try:
            result = await database.db_query("""
                DELETE FROM user_documents
                WHERE created_at < (CURRENT_TIMESTAMP - ($1 * INTERVAL '1 day'))
            """, (days_old,))
            
            # DELETE queries return the number of affected rows
            deleted_count = len(result) if result else 0
            logging.info(f"Cleaned up {deleted_count} old documents (older than {days_old} days)")
            return deleted_count
            
        except Exception as e:
            logging.error(f"Error cleaning up old documents: {e}")
            return 0
    
    async def get_document_stats(self) -> Dict[str, Any]:
        """Получает статистику документов"""
        try:
            # Размер БД (приблизительно)
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
                    'total_documents': stats['doc_count'],
                    'total_size_chars': stats['total_size'],
                    'average_size_chars': stats['avg_size'],
                    'total_size_mb': stats['total_size'] / (1024 * 1024) if stats['total_size'] else 0
                }
            
            return {'total_documents': 0, 'total_size_chars': 0, 'average_size_chars': 0, 'total_size_mb': 0}
            
        except Exception as e:
            logging.error(f"Error getting document stats: {e}")
            return {'total_documents': 0, 'total_size_chars': 0, 'average_size_chars': 0, 'total_size_mb': 0}

    async def get_user_document_stats(self, user_id: int) -> Dict[str, Any]:
        """Получает статистику документов конкретного пользователя"""
        try:
            # Количество документов пользователя
            count_result = await database.db_query(
                "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
                (user_id,)
            )
            doc_count = count_result[0]['doc_count'] if count_result else 0
            
            # Размер документов пользователя
            size_result = await database.db_query("""
                SELECT 
                    COALESCE(SUM(file_size), 0) as total_size,
                    COALESCE(AVG(file_size), 0) as avg_size
                FROM user_documents 
                WHERE user_id = $1
            """, (user_id,))
            
            if size_result:
                stats = size_result[0]
                return {
                    'document_count': doc_count,
                    'total_size_chars': stats['total_size'],
                    'average_size_chars': stats['avg_size'],
                    'total_size_mb': stats['total_size'] / (1024 * 1024) if stats['total_size'] else 0,
                    'limit_reached': doc_count >= 5,
                    'can_upload': doc_count < 5
                }
            
            return {
                'document_count': 0,
                'total_size_chars': 0,
                'average_size_chars': 0,
                'total_size_mb': 0,
                'limit_reached': False,
                'can_upload': True
            }
            
        except Exception as e:
            logging.error(f"Error getting user document stats: {e}")
            return {
                'document_count': 0,
                'total_size_chars': 0,
                'average_size_chars': 0,
                'total_size_mb': 0,
                'limit_reached': False,
                'can_upload': True
            }

# Глобальный экземпляр процессора документов
document_processor = DocumentProcessor()

async def process_uploaded_document(file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
    """Обрабатывает загруженный документ"""
    return await document_processor.process_document(file_data, filename, user_id)

async def process_uploaded_document_force(file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
    """Обрабатывает загруженный документ принудительно (игнорируя дубликаты)"""
    return await document_processor.process_document_force(file_data, filename, user_id)

async def get_user_documents(user_id: int) -> List[Dict[str, Any]]:
    """Получает документы пользователя"""
    return await document_processor.get_user_documents(user_id)

async def get_document_content(document_id: int, user_id: int) -> Optional[str]:
    """Получает содержимое документа"""
    return await document_processor.get_document_content(document_id, user_id)

async def delete_user_document(document_id: int, user_id: int) -> bool:
    """Удаляет документ пользователя"""
    return await document_processor.delete_document(document_id, user_id)

async def _upload_file_to_x0_at(file_data: bytes, filename: str) -> Optional[str]:
    """Internal function for uploading file to x0.at with retry logic."""
    timeout_config = httpx.Timeout(
        connect=10.0,  # 10 секунд на подключение
        read=60.0,     # 60 секунд на чтение (для загрузки файлов)
        write=60.0,    # 60 секунд на запись (для загрузки файлов)
        pool=30.0      # 30 секунд на получение соединения из пула
    )
    
    async with httpx.AsyncClient(timeout=timeout_config) as client:
        files = {'file': (filename, file_data)}
        response = await client.post('https://x0.at/', files=files)
        
        if response.status_code == 200:
            url = response.text.strip()
            if url.startswith('http'):
                logging.info(f"File {filename} uploaded to x0.at: {url}")
                return url
            else:
                logging.error(f"Invalid response from x0.at: {response.text}")
                return None
        else:
            logging.error(f"Failed to upload to x0.at: {response.status_code} - {response.text}")
            return None

async def upload_to_x0_at(file_data: bytes, filename: str) -> Optional[str]:
    """Загружает файл на внешний сервис x0.at и возвращает URL с автоматическими повторами"""
    try:
        return await NetworkErrorHandler.retry_with_backoff(
            _upload_file_to_x0_at, 
            max_retries=3, 
            base_delay=2.0,
            file_data=file_data, 
            filename=filename
        )
    except Exception as e:
        logging.error(f"Error uploading to x0.at after retries: {e}")
        return None

async def get_document_by_id(document_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Получает документ по ID"""
    return await document_processor.get_document_by_id(document_id, user_id) 

async def schedule_document_cleanup():
    """Планировщик автоматической очистки документов"""
    
    while True:
        try:
            # Ждем до следующего дня в 3:00 утра
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run.replace(day=next_run.day + 1)
            
            wait_seconds = (next_run - now).total_seconds()
            logging.info(f"Next document cleanup scheduled for {next_run}")
            
            await asyncio.sleep(wait_seconds)
            
            # Выполняем очистку
            deleted_count = await document_processor.cleanup_old_documents(3)
            if deleted_count > 0:
                logging.info(f"Automatic cleanup: deleted {deleted_count} old documents (older than 3 days)")
            
        except Exception as e:
            logging.error(f"Error in scheduled document cleanup: {e}")
            await asyncio.sleep(3600)  # Ждем час при ошибке

# Запускаем планировщик при старте приложения
def start_cleanup_scheduler():
    """Запускает планировщик очистки документов"""
    asyncio.create_task(schedule_document_cleanup()) 