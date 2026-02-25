"""
Shared image processing utilities for AI providers.

Extracted from services.py to be shared between GeminiProvider and OpenRouterProvider.
"""

import asyncio
import concurrent.futures
import io
import logging
import math
from typing import Optional, Union

from PIL import Image


# Global process pool for image processing outside the GIL
_image_process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=2)


def _image_worker(
    image_data: Union[bytes, Image.Image], max_size_mb: int = 10
) -> Optional[bytes]:
    """Synchronous image compression worker (runs in process pool)."""
    from PIL import Image

    try:
        from app.utils.image import estimate_image_size_in_bytes

        if isinstance(image_data, bytes):
            img_to_process = Image.open(io.BytesIO(image_data))
        else:
            img_to_process = image_data

        # Use optimized estimation
        img_bytes_approx = estimate_image_size_in_bytes(img_to_process)

        if img_bytes_approx > max_size_mb * 1024 * 1024:
            ratio = math.sqrt((max_size_mb * 1024 * 1024) / img_bytes_approx)
            new_size = tuple(int(dim * ratio) for dim in img_to_process.size)
            img_to_process = img_to_process.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        if img_to_process.mode in ("RGBA", "P"):
            img_to_process = img_to_process.convert("RGB")

        img_to_process.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logging.error("Error in image processing worker: %s", e, exc_info=True)
        return None


async def save_image_as_bytes(
    image_data: Union[bytes, Image.Image], timeout: float = 5.0, max_size_mb: int = 10
) -> Optional[bytes]:
    """Save image as bytes with timeout and compression outside the GIL."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _image_process_pool, _image_worker, image_data, max_size_mb
            ),
            timeout=timeout,
        )
    except Exception as e:
        logging.error("Image processing error: %s", e)
        return None
