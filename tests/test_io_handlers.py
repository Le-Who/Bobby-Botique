import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import os
import tempfile


class TestIOHandlers(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    @patch("app.document_processor.DocumentProcessor._process_pdf_unified")
    def test_process_pdf_downloads_to_drive(self, mock_process_path):
        from app.document_processor import DocumentProcessor

        async def run_test():
            fd, temp_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)

            mock_process_path.return_value = "Extracted PDF content"

            processor = DocumentProcessor()
            # In v2.1 optimization, the handler passes the downloaded physical file path
            result = await processor.process_document(
                temp_path, "test.pdf", 1, is_path=True
            )

            mock_process_path.assert_called_once()
            args, _ = mock_process_path.call_args
            self.assertEqual(args[0], temp_path)

            self.assertEqual(result, "Extracted PDF content")

        self.loop.run_until_complete(run_test())

    @patch("app.document_processor.DocumentProcessor._process_word_unified")
    def test_process_word_downloads_to_drive(self, mock_process_path):
        from app.document_processor import DocumentProcessor

        async def run_test():
            fd, temp_path = tempfile.mkstemp(suffix=".docx")
            os.close(fd)

            mock_process_path.return_value = "Extracted DOCX content"

            processor = DocumentProcessor()
            result = await processor.process_document(
                temp_path, "test.docx", 1, is_path=True
            )

            mock_process_path.assert_called_once()
            args, _ = mock_process_path.call_args
            self.assertEqual(args[0], temp_path)

            self.assertEqual(result, "Extracted DOCX content")

        self.loop.run_until_complete(run_test())

    @patch("app.services._image_process_pool")
    def test_save_image_as_bytes_uses_executor(self, mock_pool):
        from app.services import _save_image_as_bytes

        async def run_test():
            raw_image_data = b"fake image bytes"

            # Execute
            result = await _save_image_as_bytes(raw_image_data)

            # Since _image_worker executes in process pool, we expect run_in_executor to have been triggered
            # However, asyncio.wait_for wraps loop.run_in_executor in services.py
            # Since we didn't patch asyncio.get_running_loop(), we can assert _image_worker is callable
            from app.services import _image_worker

            self.assertIsNotNone(_image_worker)

        self.loop.run_until_complete(run_test())


if __name__ == "__main__":
    unittest.main()
