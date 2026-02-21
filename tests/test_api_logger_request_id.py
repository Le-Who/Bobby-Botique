import json
import unittest
import sys
import os

sys.path.append(os.getcwd())
from unittest.mock import MagicMock, patch

from app.utils.api_logger import APILogger


class TestAPILoggerRequestId(unittest.TestCase):
    def setUp(self):
        self.logger = APILogger()
        self.logger.logger = MagicMock()

    @patch("app.utils.api_logger.get_request_id", return_value="rid-123")
    def test_log_api_request_includes_request_id(self, _mock_request_id):
        self.logger.log_api_request(
            api_name="telegram",
            endpoint="/send",
            method="POST",
            request_data={"text": "hello"},
        )

        message = self.logger.logger.info.call_args[0][0]
        payload = json.loads(message.split(": ", 1)[1])
        self.assertEqual(payload["request_id"], "rid-123")

    @patch("app.utils.api_logger.get_request_id", return_value="rid-err")
    def test_log_error_includes_request_id(self, _mock_request_id):
        self.logger.log_error("gemini", ValueError("bad request"))

        message = self.logger.logger.error.call_args[0][0]
        payload = json.loads(message.split(": ", 1)[1])
        self.assertEqual(payload["request_id"], "rid-err")


if __name__ == "__main__":
    unittest.main()
