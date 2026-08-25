import unittest
from importlib import reload
from unittest.mock import patch

from PIL import Image

import app.utils.image_utils as image_utils
from app.utils.image import estimate_image_size_in_bytes
from app.utils.image_utils import _image_worker


class TestImageUtils(unittest.TestCase):
    def tearDown(self):
        image_utils.shutdown_image_pool()
        image_utils._image_process_pool_failed = False

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

    def test_import_does_not_create_process_pool(self):
        # ``reload`` recreates dataclasses, so restore the public TaggedImage
        # class afterwards. Otherwise other test modules collected earlier can
        # hold the old class while provider code imports the new one.
        tagged_image_class = image_utils.TaggedImage
        try:
            with patch("concurrent.futures.ProcessPoolExecutor", side_effect=AssertionError("pool should stay lazy")):
                reload(image_utils)
        finally:
            image_utils.TaggedImage = tagged_image_class

    def test_get_image_process_pool_is_lazy_and_singleton(self):
        sentinel_pool = object()
        image_utils._image_process_pool = None
        image_utils._image_process_pool_failed = False

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=sentinel_pool) as mock_ctor:
            first = image_utils._get_image_process_pool()
            second = image_utils._get_image_process_pool()

        self.assertIs(first, sentinel_pool)
        self.assertIs(second, sentinel_pool)
        mock_ctor.assert_called_once()

    def test_get_image_process_pool_falls_back_gracefully_on_failure(self):
        image_utils._image_process_pool = None
        image_utils._image_process_pool_failed = False

        with patch("concurrent.futures.ProcessPoolExecutor", side_effect=PermissionError("denied")):
            pool = image_utils._get_image_process_pool()

        self.assertIsNone(pool)
        self.assertTrue(image_utils._image_process_pool_failed)


if __name__ == "__main__":
    unittest.main()
