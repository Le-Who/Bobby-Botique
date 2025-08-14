import logging
import io
import asyncio
import hashlib
import httpx
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
import tempfile
import os

# Импорты для работы с документами
try:
    import PyPDF2
    import docx
    from PIL import Image
    import fitz  # PyMuPDF для более качественного извлечения PDF
    DOCUMENT_SUPPORT = True
except ImportError:
    DOCUMENT_SUPPORT = False
    logging.warning("Document processing libraries not installed. Document support disabled.")

from .config import settings
from . import database as db
from .metrics import metrics_collector

class DocumentProcessor:
    """Процессор для обработки документов"""
    
    def __init__(self):
        self.supported_formats = ['.pdf', '.docx', '.doc']
        self.max_file_size = 50 * 1024 * 1024  # 50MB
        self.max_pages = 100  # Максимальное количество страниц
    
    def _calculate_file_hash(self, file_data: bytes) -> str:
        """Вычисляет SHA-256 хэш файла"""
        return hashlib.sha256(file_data).hexdigest()
    
    async def _check_duplicate_file(self, user_id: int, file_hash: str, filename: str) -> Optional[Dict[str, Any]]:
        """Проверяет, есть ли уже такой файл у пользователя"""
        try:
            result = await db.db_query(
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
            result = await db.db_query(
                "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
                (user_id,)
            )
            doc_count = result[0]['doc_count'] if result else 0
            # Возвращаем True, если лимит НЕ превышен (можно загружать документы)
            return doc_count < 5  # Максимум 5 документов на пользователя
        except Exception as e:
            logging.error(f"Error checking document limit: {e}")
            return True  # В случае ошибки разрешаем загрузку
    
    async def _cleanup_oldest_documents(self, user_id: int, keep_count: int = 4) -> int:
        """Удаляет старые документы пользователя, оставляя указанное количество"""
        try:
            # Сначала получаем общее количество документов пользователя
            count_result = await db.db_query(
                "SELECT COUNT(*) as total_count FROM user_documents WHERE user_id = $1",
                (user_id,)
            )
            total_count = count_result[0]['total_count'] if count_result else 0
            
            if total_count <= keep_count:
                return 0  # Нечего удалять
            
            # Вычисляем, сколько документов нужно удалить
            docs_to_delete = total_count - keep_count
            
            # Получаем ID самых старых документов для удаления
            result = await db.db_query("""
                SELECT id FROM user_documents 
                WHERE user_id = $1 
                ORDER BY created_at ASC
                LIMIT $2
            """, (user_id, docs_to_delete))
            
            if not result:
                return 0
            
            # Удаляем старые документы
            old_doc_ids = [row['id'] for row in result]
            if old_doc_ids:
                # Создаем правильные placeholder'ы для PostgreSQL
                placeholders = ','.join([f'${i+1}' for i in range(len(old_doc_ids))])
                await db.db_query(f"""
                    DELETE FROM user_documents 
                    WHERE id IN ({placeholders})
                """, old_doc_ids)
                
                deleted_count = len(old_doc_ids)
                logging.info(f"Cleaned up {deleted_count} oldest documents for user {user_id}")
                return deleted_count
            
            return 0
            
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
            file_hash = self._calculate_file_hash(file_data)
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
            file_hash = self._calculate_file_hash(file_data)
            
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
            # Создаем временный файл для обработки
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
                temp_file.write(file_data)
                temp_file_path = temp_file.name
            
            try:
                # Проверяем, что файл является корректным PDF
                if not file_data.startswith(b'%PDF'):
                    logging.warning(f"Invalid PDF format for {filename}")
                    return {"error": "Invalid PDF file format"}
                
                logging.info(f"Processing PDF {filename} with PyMuPDF")
                
                # Используем PyMuPDF для лучшего извлечения текста
                try:
                    doc = fitz.open(temp_file_path)
                    logging.info(f"Successfully opened PDF {filename} with {len(doc)} pages")
                except Exception as fitz_error:
                    logging.warning(f"PyMuPDF failed for {filename}, trying PyPDF2: {fitz_error}")
                    # Fallback на PyPDF2
                    return await self._process_pdf_with_pypdf2(file_data, filename, user_id, file_hash)
                
                if len(doc) > self.max_pages:
                    return {"error": f"PDF too large. Maximum {self.max_pages} pages allowed"}
                
                text_content = []
                page_info = []
                
                for page_num in range(len(doc)):
                    page = doc.load_page(page_num)
                    
                    # Извлекаем текст
                    text = page.get_text()
                    if text.strip():
                        text_content.append(f"--- Page {page_num + 1} ---\n{text}")
                    
                    # Извлекаем изображения (если есть)
                    image_list = page.get_images()
                    if image_list:
                        page_info.append(f"Page {page_num + 1}: {len(image_list)} images found")
                    
                    # Проверяем лимит токенов
                    if len('\n'.join(text_content)) > 100000:  # Примерный лимит
                        text_content.append(f"\n--- Document truncated at page {page_num + 1} ---")
                        break
                
                # Сохраняем количество страниц до закрытия документа
                page_count = len(doc)
                
                # Закрываем документ
                doc.close()
                
                full_text = '\n\n'.join(text_content)
                
                # Сохраняем в базу данных
                await self._save_document_content(user_id, filename, full_text, page_count, file_hash)
                
                return {
                    "success": True,
                    "filename": filename,
                    "pages": page_count,
                    "text_length": len(full_text),
                    "content": full_text,
                    "page_info": page_info
                }
                
            finally:
                # Удаляем временный файл
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
            
        except Exception as e:
            logging.error(f"Error processing PDF {filename}: {e}", exc_info=True)
            await metrics_collector.record_error("pdf_processing", str(e))
            return {"error": f"Error processing PDF: {str(e)}"}
    
    async def _process_pdf_with_pypdf2(self, file_data: bytes, filename: str, user_id: int, file_hash: str) -> Dict[str, Any]:
        """Обрабатывает PDF документ с использованием PyPDF2 (fallback)"""
        try:
            # Создаем временный файл для обработки
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
                temp_file.write(file_data)
                temp_file_path = temp_file.name
            
            try:
                # Используем PyPDF2 как fallback
                try:
                    with open(temp_file_path, 'rb') as file:
                        pdf_reader = PyPDF2.PdfReader(file)
                except Exception as pdf_error:
                    logging.error(f"PyPDF2 failed to open {filename}: {pdf_error}")
                    return {"error": f"Failed to open PDF file: {str(pdf_error)}"}
                
                if len(pdf_reader.pages) > self.max_pages:
                    return {"error": f"PDF too large. Maximum {self.max_pages} pages allowed"}
                
                text_content = []
                
                for page_num, page in enumerate(pdf_reader.pages):
                    try:
                        text = page.extract_text()
                        if text.strip():
                            text_content.append(f"--- Page {page_num + 1} ---\n{text}")
                    except Exception as page_error:
                        logging.warning(f"Error extracting text from page {page_num + 1}: {page_error}")
                        text_content.append(f"--- Page {page_num + 1} ---\n[Error extracting text from this page]")
                    
                    # Проверяем лимит токенов
                    if len('\n'.join(text_content)) > 100000:
                        text_content.append(f"\n--- Document truncated at page {page_num + 1} ---")
                        break
                
                full_text = '\n\n'.join(text_content)
                
                # Сохраняем в базу данных
                await self._save_document_content(user_id, filename, full_text, len(pdf_reader.pages), file_hash)
                
                return {
                    "success": True,
                    "filename": filename,
                    "pages": len(pdf_reader.pages),
                    "text_length": len(full_text),
                    "content": full_text,
                    "method": "PyPDF2"
                }
                    
            finally:
                # Удаляем временный файл
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                    
        except Exception as e:
            logging.error(f"Error processing PDF with PyPDF2 {filename}: {e}", exc_info=True)
            await metrics_collector.record_error("pdf_processing_pypdf2", str(e))
            return {"error": f"Error processing PDF with PyPDF2: {str(e)}"}
    
    async def _process_word(self, file_data: bytes, filename: str, user_id: int, file_hash: str) -> Dict[str, Any]:
        """Обрабатывает Word документ"""
        try:
            # Создаем временный файл
            with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as temp_file:
                temp_file.write(file_data)
                temp_file_path = temp_file.name
            
            try:
                doc = docx.Document(temp_file_path)
                
                text_content = []
                paragraph_count = 0
                
                # Извлекаем текст из параграфов
                for para in doc.paragraphs:
                    if para.text.strip():
                        text_content.append(para.text)
                        paragraph_count += 1
                
                # Извлекаем текст из таблиц
                table_count = 0
                for table in doc.tables:
                    table_count += 1
                    text_content.append(f"\n--- Table {table_count} ---")
                    for row in table.rows:
                        row_text = []
                        for cell in row.cells:
                            if cell.text.strip():
                                row_text.append(cell.text.strip())
                        if row_text:
                            text_content.append(" | ".join(row_text))
                
                full_text = '\n\n'.join(text_content)
                
                # Сохраняем в базу данных
                await self._save_document_content(user_id, filename, full_text, 1, file_hash)  # Word документы считаем как 1 страницу
                
                return {
                    "success": True,
                    "filename": filename,
                    "pages": 1,
                    "paragraphs": paragraph_count,
                    "tables": table_count,
                    "text_length": len(full_text),
                    "content": full_text
                }
                
            finally:
                # Удаляем временный файл
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                    
        except Exception as e:
            logging.error(f"Error processing Word document {filename}: {e}", exc_info=True)
            await metrics_collector.record_error("word_processing", str(e))
            return {"error": f"Error processing Word document: {str(e)}"}
    
    async def _save_document_content(self, user_id: int, filename: str, content: str, pages: int, file_hash: str):
        """Сохраняет содержимое документа в базу данных"""
        try:
            # Таблица уже создана в init_db, поэтому просто сохраняем документ
            await db.db_query(
                "INSERT INTO user_documents (user_id, filename, content, pages, file_size, file_hash) VALUES ($1, $2, $3, $4, $5, $6)",
                (user_id, filename, content, pages, len(content), file_hash)
            )
            
            logging.info(f"Saved document {filename} for user {user_id}")
            
        except Exception as e:
            logging.error(f"Error saving document to database: {e}")
    
    async def get_document_by_id(self, document_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """Получает документ по ID"""
        try:
            result = await db.db_query(
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
            result = await db.db_query(
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
            
        except Exception as e:
            logging.error(f"Error getting user documents: {e}")
            return []
    
    async def get_document_content(self, document_id: int, user_id: int) -> Optional[str]:
        """Получает содержимое документа"""
        try:
            result = await db.db_query(
                "SELECT content FROM user_documents WHERE id = $1 AND user_id = $2",
                (document_id, user_id)
            )
            
            if result:
                return result[0]['content']
            return None
            
        except Exception as e:
            logging.error(f"Error getting document content: {e}")
            return None
    
    async def delete_document(self, document_id: int, user_id: int) -> bool:
        """Удаляет документ"""
        try:
            await db.db_query(
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
            # Сначала считаем количество документов для удаления
            count_result = await db.db_query("""
                SELECT COUNT(*) as count_to_delete FROM user_documents 
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
            """, (days_old,))
            
            count_to_delete = count_result[0]['count_to_delete'] if count_result else 0
            
            if count_to_delete > 0:
                # Теперь удаляем документы
                await db.db_query("""
                    DELETE FROM user_documents 
                    WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '1 day' * $1
                """, (days_old,))
                
                logging.info(f"Cleaned up {count_to_delete} old documents (older than {days_old} days)")
                return count_to_delete
            else:
                logging.info(f"No documents to clean up (older than {days_old} days)")
                return 0
            
        except Exception as e:
            logging.error(f"Error cleaning up old documents: {e}")
            return 0
    
    async def get_document_stats(self) -> Dict[str, Any]:
        """Получает статистику документов"""
        try:
            # Общее количество документов
            total_result = await db.db_query("SELECT COUNT(*) as total FROM user_documents")
            total_docs = total_result[0]['total'] if total_result else 0
            
            # Размер БД (приблизительно)
            size_result = await db.db_query("""
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
            count_result = await db.db_query(
                "SELECT COUNT(*) as doc_count FROM user_documents WHERE user_id = $1",
                (user_id,)
            )
            doc_count = count_result[0]['doc_count'] if count_result else 0
            
            # Размер документов пользователя
            size_result = await db.db_query("""
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

async def upload_to_x0_at(file_data: bytes, filename: str) -> Optional[str]:
    """Загружает файл на внешний сервис x0.at и возвращает URL"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
                
    except Exception as e:
        logging.error(f"Error uploading to x0.at: {e}")
        return None

async def get_document_by_id(document_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Получает документ по ID"""
    return await document_processor.get_document_by_id(document_id, user_id) 

async def schedule_document_cleanup():
    """Планировщик автоматической очистки документов"""
    import asyncio
    from datetime import datetime
    
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
    import asyncio
    asyncio.create_task(schedule_document_cleanup()) 