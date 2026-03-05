import json
import os
import sys
import unittest

sys.path.append(os.getcwd())
from unittest.mock import MagicMock, patch

from app.utils.api_logger import APILogger


class TestAPILoggerRequestId(unittest.TestCase):
    def setUp(self):
        self.logger = APILogger()
        self.logger.logger = MagicMock()

    @patch("app.utils.api_logger.get_request_id", return_value="rid-123")
    @patch("app.utils.api_logger.get_user_id", return_value=42)
    @patch("app.utils.api_logger.get_chat_id", return_value=100)
    def test_log_request_includes_context_fields(self, _chat, _user, _rid):
        self.logger.log_request("telegram", endpoint="/send", method="POST")

        # logger.info("%s %s REQUEST STARTED: %s", emoji, api, json)
        message = self.logger.logger.info.call_args[0][3]
        payload = json.loads(message)
        self.assertEqual(payload["request_id"], "rid-123")
        self.assertEqual(payload["user_id"], 42)
        self.assertEqual(payload["chat_id"], 100)
        self.assertEqual(payload["api"], "telegram")

    @patch("app.utils.api_logger.get_request_id", return_value="rid-err")
    @patch("app.utils.api_logger.get_user_id", return_value=None)
    @patch("app.utils.api_logger.get_chat_id", return_value=None)
    def test_log_error_includes_request_id(self, _chat, _user, _rid):
        self.logger.log_error("gemini", ValueError("bad request"))

        # logger.error("%s API ERROR: %s", emoji, json)
        message = self.logger.logger.error.call_args[0][2]
        payload = json.loads(message)
        self.assertEqual(payload["request_id"], "rid-err")
        self.assertEqual(payload["api"], "gemini")
        self.assertEqual(payload["error_type"], "ValueError")

    @patch("app.utils.api_logger.get_request_id", return_value="rid-resp")
    @patch("app.utils.api_logger.get_user_id", return_value=7)
    @patch("app.utils.api_logger.get_chat_id", return_value=77)
    def test_log_response_success(self, _chat, _user, _rid):
        import time

        start = time.time() - 0.5  # simulate 500ms elapsed
        self.logger.log_response("gemini", start, model="gemini-2.5-flash", response_length=100)

        # logger.info("%s %s RESPONSE COMPLETED: %s", emoji, api, json)
        message = self.logger.logger.info.call_args[0][3]
        payload = json.loads(message)
        self.assertEqual(payload["request_id"], "rid-resp")
        self.assertEqual(payload["user_id"], 7)
        self.assertTrue(payload["success"])
        self.assertIn("duration_ms", payload)
        self.assertEqual(payload["model"], "gemini-2.5-flash")

    @patch("app.utils.api_logger.get_request_id", return_value="rid-fail")
    @patch("app.utils.api_logger.get_user_id", return_value=None)
    @patch("app.utils.api_logger.get_chat_id", return_value=None)
    def test_log_response_failure(self, _chat, _user, _rid):
        import time

        start = time.time()
        self.logger.log_response("tavily", start, success=False, error_message="timeout")

        # logger.error("%s %s RESPONSE FAILED: %s", emoji, api, json)
        message = self.logger.logger.error.call_args[0][3]
        payload = json.loads(message)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_message"], "timeout")


if __name__ == "__main__":
    unittest.main()
