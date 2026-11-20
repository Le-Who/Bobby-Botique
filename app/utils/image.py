from PIL import Image


def estimate_image_size_in_bytes(image: Image.Image) -> int:
    """
    Estimates the memory size of an image in bytes without fully decompressing it.
    This replaces len(image.tobytes()) which is O(N) and memory intensive.

    Args:
        image: A PIL Image object.

    Returns:
        Estimated size in bytes.
    """
    width, height = image.size
    mode = image.mode

    if mode in ("1", "L", "P"):
        bpp = 1
    elif mode in ("RGB", "YCbCr", "LAB", "HSV"):
        bpp = 3
    elif mode in ("RGBA", "CMYK", "I", "F"):
        bpp = 4
    elif mode == "I;16":
        bpp = 2
    else:
        try:
            bpp = len(image.getbands())
        except Exception:
            return len(image.tobytes())

    return width * height * bpp
