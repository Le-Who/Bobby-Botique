import unittest
from PIL import Image
from app.utils.image import estimate_image_size_in_bytes

class TestImageUtils(unittest.TestCase):
    def test_estimate_image_size_rgb(self):
        width, height = 100, 100
        image = Image.new('RGB', (width, height))
        expected_size = width * height * 3
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_rgba(self):
        width, height = 50, 50
        image = Image.new('RGBA', (width, height))
        expected_size = width * height * 4
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_l(self):
        width, height = 200, 100
        image = Image.new('L', (width, height))
        expected_size = width * height * 1
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_1(self):
        width, height = 200, 100
        image = Image.new('1', (width, height))
        # For mode '1', we estimate 1 byte per pixel even though packed is less
        expected_size = width * height * 1
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

    def test_estimate_image_size_cmyk(self):
        width, height = 10, 10
        image = Image.new('CMYK', (width, height))
        expected_size = width * height * 4
        estimated_size = estimate_image_size_in_bytes(image)
        self.assertEqual(estimated_size, expected_size)

if __name__ == '__main__':
    unittest.main()
