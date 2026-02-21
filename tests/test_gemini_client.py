import unittest
from unittest.mock import MagicMock
from app.gemini_client import GeminiClient


class TestGeminiClient(unittest.TestCase):
    def setUp(self):
        self.client = GeminiClient()

    def test_rotate_key_success(self):
        """Test _rotate_key when a key is available."""
        model_name = "test-model"

        # Mock _find_available_key to return a valid key
        self.client._find_available_key = MagicMock(return_value="valid_key")

        # Mock _set_current_key
        self.client._set_current_key = MagicMock()

        # Call the method
        self.client._rotate_key(model_name)

        # Assertions
        self.client._find_available_key.assert_called_once_with(model_name)
        self.client._set_current_key.assert_called_once()

    def test_rotate_key_no_key_available(self):
        """Test _rotate_key when no key is available."""
        model_name = "test-model"

        # Mock _find_available_key to return None
        self.client._find_available_key = MagicMock(return_value=None)

        # Mock _set_current_key
        self.client._set_current_key = MagicMock()

        # Call the method and expect exception
        with self.assertRaises(Exception) as context:
            self.client._rotate_key(model_name)

        self.assertIn(
            f"No available API keys for model {model_name}", str(context.exception)
        )

        # Assertions
        self.client._find_available_key.assert_called_once_with(model_name)
        self.client._set_current_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
