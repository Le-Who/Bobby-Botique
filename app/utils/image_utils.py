"""
Shared image processing utilities for AI providers.

Extracted from services.py to be shared between GeminiProvider and OpenRouterProvider.
"""

import asyncio
import concurrent.futures
import io
import logging
import math
from dataclasses import dataclass

from cachetools import TTLCache
from PIL import Image


@dataclass(frozen=True)
class TaggedImage:
    """Image bytes with optional processing hints for providers.

    Use this to carry metadata (cache key, task type, pre-compression flag)
    across the handler → provider boundary via history ``parts``.

    Providers check ``isinstance(part, TaggedImage)`` and use the hints
    to avoid redundant recompression.
    """

    data: bytes
    cache_key: str | None = None
    task_type: str = "default"
    pre_compressed: bool = False  # True → provider skips recompression


# Global process pool for image processing outside the GIL.
# max_tasks_per_child=1 ensures each worker process exits after handling one
# image, so accumulated PIL/pymalloc memory is returned to the OS immediately.
_image_process_pool = concurrent.futures.ProcessPoolExecutor(
    max_workers=2,
    max_tasks_per_child=1,
)

# TTL cache for compressed images — avoids reprocessing on retries/follow-ups.
# Keyed by cache_key (e.g. Telegram file_unique_id). Max 50 entries, 3 min TTL.
_compressed_cache: TTLCache[str, bytes] = TTLCache(maxsize=50, ttl=180)

# ── Task-aware dimension caps ────────────────────────────────────────────────
# Maps task_type → max dimension (longest side).
# These values balance token cost vs. visual detail for each use case.
TASK_DIMS: dict[str, int] = {
    "describe": 1280,  # General description — good detail at moderate cost
    "search": 768,  # Search query extraction — content matters, not pixels
    "ocr": 2048,  # Text recognition — needs higher resolution
    "default": 1280,  # Fallback
}

# Fallback JPEG quality levels when the result exceeds max_size_mb
_FALLBACK_QUALITIES = (75, 65)


def _image_worker(
    image_data: bytes | Image.Image,
    max_size_mb: int = 10,
    task_type: str = "default",
) -> bytes | None:
    """Synchronous image compression worker (runs in process pool).

    Three-stage pipeline:
      1. Resolution cap — ``thumbnail()`` to ``TASK_DIMS[task_type]`` max side.
      2. Format normalisation — RGBA/P → RGB, save as JPEG quality 85.
      3. Fallback quality reduction — if file still > ``max_size_mb``, retry at
         lower JPEG quality levels.
    """
    from PIL import Image

    try:
        from app.utils.image import estimate_image_size_in_bytes

        img_to_process = Image.open(io.BytesIO(image_data)) if isinstance(image_data, bytes) else image_data

        # Stage 1: Resolution cap (token savings)
        max_dim = TASK_DIMS.get(task_type, TASK_DIMS["default"])
        if max(img_to_process.size) > max_dim:
            img_to_process.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        # Legacy size-based resize for very large estimated sizes
        img_bytes_approx = estimate_image_size_in_bytes(img_to_process)
        if img_bytes_approx > max_size_mb * 1024 * 1024:
            ratio = math.sqrt((max_size_mb * 1024 * 1024) / img_bytes_approx)
            new_dims = [int(dim * ratio) for dim in img_to_process.size]
            img_to_process = img_to_process.resize((new_dims[0], new_dims[1]), Image.Resampling.LANCZOS)

        # Stage 2: Format normalisation + JPEG compression
        if img_to_process.mode in ("RGBA", "P"):
            img_to_process = img_to_process.convert("RGB")

        buf = io.BytesIO()
        img_to_process.save(buf, format="JPEG", quality=85, optimize=True)
        result = buf.getvalue()

        # Stage 3: Fallback quality reduction if still too large
        max_bytes = max_size_mb * 1024 * 1024
        if len(result) > max_bytes:
            for q in _FALLBACK_QUALITIES:
                buf = io.BytesIO()
                img_to_process.save(buf, format="JPEG", quality=q, optimize=True)
                result = buf.getvalue()
                if len(result) <= max_bytes:
                    break

        return result
    except Exception as e:
        logging.error("Error in image processing worker: %s", e, exc_info=True)
        return None


async def save_image_as_bytes(
    image_data: bytes | Image.Image,
    timeout: float = 5.0,
    max_size_mb: int = 10,
    task_type: str = "default",
    cache_key: str | None = None,
) -> bytes | None:
    """Save image as bytes with timeout and compression outside the GIL.

    Args:
        cache_key: Optional key (e.g. ``file_unique_id``) for caching the
            compressed result.  Repeated calls with the same key skip
            the CPU-intensive compression step.
    """
    if cache_key and cache_key in _compressed_cache:
        return _compressed_cache[cache_key]

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_image_process_pool, _image_worker, image_data, max_size_mb, task_type),
            timeout=timeout,
        )
        if cache_key and result is not None:
            _compressed_cache[cache_key] = result
        return result
    except Exception as e:
        logging.error("Image processing error: %s", e, exc_info=True)
        return None


def shutdown_image_pool() -> None:
    """Shut down the global image process pool (call during bot shutdown)."""
    global _image_process_pool
    try:
        _image_process_pool.shutdown(wait=False, cancel_futures=True)
        logging.info("Image process pool shut down")
    except Exception as e:
        logging.warning("Error shutting down image process pool: %s", e)


def clear_image_cache() -> None:
    """Clear the compressed-image TTL cache (MemoryManager callback)."""
    count = len(_compressed_cache)
    _compressed_cache.clear()
    if count:
        logging.info("Cleared %d entries from compressed image cache", count)
