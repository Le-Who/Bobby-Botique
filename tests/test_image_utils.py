import unittest
from unittest.mock import patch

from PIL import Image

from app.utils.image import estimate_image_size_in_bytes
from app.utils.image_utils import _image_worker


class TestImageUtils(unittest.TestCase):
    def test_estimate_image_size_rgb(self):
        width, height = 100, 100
        image = Image.new("RGB", (width, height))
        expected_size = width * height * 3
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_rgba(self):
        width, height = 50, 50
        image = Image.new("RGBA", (width, height))
        expected_size = width * height * 4
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_l(self):
        width, height = 200, 100
        image = Image.new("L", (width, height))
        expected_size = width * height * 1
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_1(self):
        width, height = 200, 100
        image = Image.new("1", (width, height))
        # For mode '1', we estimate 1 byte per pixel even though packed is less
        expected_size = width * height * 1
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_cmyk(self):
        width, height = 10, 10
        image = Image.new("CMYK", (width, height))
        expected_size = width * height * 4
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    @patch("app.utils.image_utils.logging.error")
    def test_image_worker_error_path(self, mock_error):
        # Pass invalid byte data to trigger an exception in Image.open
        result = _image_worker(b"invalid_image_data")

        # Verify that it returns None on exception
        self.assertIsNone(result)

        # Verify that logging.error was called
        mock_error.assert_called_once()
        args, kwargs = mock_error.call_args
        self.assertIn("Error in image processing worker: %s", args[0])
        self.assertTrue(kwargs.get("exc_info"))


if __name__ == "__main__":
    unittest.main()
