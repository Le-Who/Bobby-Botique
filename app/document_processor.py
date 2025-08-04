import logging
import io
import asyncio
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
            
            # Обрабатываем документ
            if file_ext == '.pdf':
                return await self._process_pdf(file_data, filename, user_id)
            elif file_ext in ['.docx', '.doc']:
                return await self._process_word(file_data, filename, user_id)
            else:
                return {"error": f"Unsupported file format: {file_ext}"}
                
        except Exception as e:
            logging.error(f"Error processing document {filename}: {e}")
            await metrics_collector.record_error("document_processing", str(e))
            return {"error": f"Error processing document: {str(e)}"}
    
    async def _process_pdf(self, file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
        """Обрабатывает PDF документ"""
        try:
            # Используем PyMuPDF для лучшего извлечения текста
            doc = fitz.open(stream=file_data, filetype="pdf")
            
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
            
            doc.close()
            
            full_text = '\n\n'.join(text_content)
            
            # Сохраняем в базу данных
            await self._save_document_content(user_id, filename, full_text, len(doc))
            
            return {
                "success": True,
                "filename": filename,
                "pages": len(doc),
                "text_length": len(full_text),
                "content": full_text,
                "page_info": page_info
            }
            
        except Exception as e:
            logging.error(f"Error processing PDF {filename}: {e}")
            return {"error": f"Error processing PDF: {str(e)}"}
    
    async def _process_word(self, file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
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
                await self._save_document_content(user_id, filename, full_text, 1)  # Word документы считаем как 1 страницу
                
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
            logging.error(f"Error processing Word document {filename}: {e}")
            return {"error": f"Error processing Word document: {str(e)}"}
    
    async def _save_document_content(self, user_id: int, filename: str, content: str, pages: int):
        """Сохраняет содержимое документа в базу данных"""
        try:
            # Создаем таблицу для документов, если её нет
            await db.db_query("""
                CREATE TABLE IF NOT EXISTS user_documents (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    filename TEXT NOT NULL,
                    content TEXT,
                    pages INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    file_size INTEGER
                )
            """)
            
            # Сохраняем документ
            await db.db_query(
                "INSERT INTO user_documents (user_id, filename, content, pages, file_size) VALUES (?, ?, ?, ?, ?)",
                (user_id, filename, content, pages, len(content))
            )
            
            logging.info(f"Saved document {filename} for user {user_id}")
            
        except Exception as e:
            logging.error(f"Error saving document to database: {e}")
    
    async def get_user_documents(self, user_id: int) -> List[Dict[str, Any]]:
        """Получает список документов пользователя"""
        try:
            result = await db.db_query(
                "SELECT id, filename, pages, created_at, file_size FROM user_documents WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)
            )
            
            return [
                {
                    'id': row['id'],
                    'filename': row['filename'],
                    'pages': row['pages'],
                    'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                    'file_size': row['file_size']
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
                "SELECT content FROM user_documents WHERE id = ? AND user_id = ?",
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
                "DELETE FROM user_documents WHERE id = ? AND user_id = ?",
                (document_id, user_id)
            )
            return True
            
        except Exception as e:
            logging.error(f"Error deleting document: {e}")
            return False

# Глобальный экземпляр процессора документов
document_processor = DocumentProcessor()

async def process_uploaded_document(file_data: bytes, filename: str, user_id: int) -> Dict[str, Any]:
    """Обрабатывает загруженный документ"""
    return await document_processor.process_document(file_data, filename, user_id)

async def get_user_documents(user_id: int) -> List[Dict[str, Any]]:
    """Получает документы пользователя"""
    return await document_processor.get_user_documents(user_id)

async def get_document_content(document_id: int, user_id: int) -> Optional[str]:
    """Получает содержимое документа"""
    return await document_processor.get_document_content(document_id, user_id)

async def delete_user_document(document_id: int, user_id: int) -> bool:
    """Удаляет документ пользователя"""
    return await document_processor.delete_document(document_id, user_id) 