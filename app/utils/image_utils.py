"""
Shared image processing utilities for AI providers.

Extracted from services.py to be shared between GeminiProvider and OpenRouterProvider.
"""

import asyncio
import concurrent.futures
import io
import logging
import math
import multiprocessing
import threading
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from functools import partial

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
# Lazily initialized to keep module imports safe in restricted environments.
_image_process_pool: concurrent.futures.ProcessPoolExecutor | None = None
_image_process_pool_failed = False
_image_pool_lock = threading.RLock()
_image_pool_jobs = 0
_image_pool_idle_timer: threading.Timer | None = None
_IMAGE_POOL_IDLE_SECONDS = 60.0

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


def _get_image_process_pool() -> concurrent.futures.ProcessPoolExecutor | None:
    """Return the shared image process pool, creating it on first use.

    Returns None if process-pool creation is unavailable in the current environment.
    Callers should gracefully fall back to the loop's default thread executor.
    """
    global _image_process_pool, _image_process_pool_failed
    with _image_pool_lock:
        if _image_process_pool is not None:
            return _image_process_pool
        if _image_process_pool_failed:
            return None

        try:
            _image_process_pool = concurrent.futures.ProcessPoolExecutor(
                max_workers=2,
                mp_context=multiprocessing.get_context("spawn"),
            )
        except Exception as e:
            _image_process_pool_failed = True
            logging.warning("Image process pool unavailable, falling back to thread executor: %s", e)
            return None

        return _image_process_pool


def _cancel_image_idle_timer_locked() -> None:
    global _image_pool_idle_timer
    if _image_pool_idle_timer is not None:
        _image_pool_idle_timer.cancel()
        _image_pool_idle_timer = None


def _detach_image_pool_locked(pool: concurrent.futures.ProcessPoolExecutor) -> bool:
    """Retire only this generation; caller holds the lifecycle lock."""
    global _image_process_pool, _image_pool_jobs
    if _image_process_pool is not pool:
        return False
    _cancel_image_idle_timer_locked()
    _image_process_pool = None
    _image_pool_jobs = 0
    return True


def _close_image_executor(pool: concurrent.futures.ProcessPoolExecutor) -> None:
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception as error:
        logging.warning("Image process pool shutdown failed: %s", type(error).__name__)


def _image_job_finished(
    pool: concurrent.futures.ProcessPoolExecutor, future: concurrent.futures.Future[bytes | None]
) -> None:
    """Observe real worker completion, including work whose await was cancelled."""
    global _image_pool_jobs, _image_pool_idle_timer
    broken = not future.cancelled() and isinstance(future.exception(), BrokenProcessPool)
    retire = False
    with _image_pool_lock:
        if _image_process_pool is not pool:
            return
        _image_pool_jobs -= 1
        if broken:
            # CPython delivers BrokenProcessPool while holding the executor's
            # shutdown lock. Its manager owns termination/queue cleanup/join;
            # calling shutdown here would deadlock that same manager thread.
            _detach_image_pool_locked(pool)
        elif _image_pool_jobs == 0:
            _cancel_image_idle_timer_locked()

            def expire() -> None:
                with _image_pool_lock:
                    # cancel() may race a callback already entering the lock.
                    if _image_pool_idle_timer is not timer or _image_pool_jobs:
                        return
                    expired = _detach_image_pool_locked(pool)
                if expired:
                    _close_image_executor(pool)

            timer = threading.Timer(_IMAGE_POOL_IDLE_SECONDS, expire)
            timer.daemon = True
            _image_pool_idle_timer = timer
            try:
                timer.start()
            except RuntimeError:
                # A restricted runtime may reject new threads. Release the idle
                # pool immediately rather than leaking it or failing delivery.
                retire = _detach_image_pool_locked(pool)
    if retire:
        _close_image_executor(pool)


def _image_worker(
    image_data: bytes | Image.Image,
    max_size_mb: int = 10,
    task_type: str = "default",
) -> bytes | None:
    """Synchronous image compression worker (runs in process pool).

    Four-stage pipeline:
      1. Entropy analysis — Shannon entropy of the luminance channel adjusts
         the resolution cap: text-dense images (screenshots, documents) get
         higher resolution; simple photos get lower to save tokens.
      2. Resolution cap — ``thumbnail()`` to adaptive ``max_dim``.
      3. Format normalisation — RGBA/P → RGB, save as JPEG quality 85.
      4. Fallback quality reduction — if file still > ``max_size_mb``, retry at
         lower JPEG quality levels.
    """
    from PIL import Image

    try:
        from app.utils.image import estimate_image_size_in_bytes

        img_to_process = Image.open(io.BytesIO(image_data)) if isinstance(image_data, bytes) else image_data

        # Stage 1: Entropy-based adaptive max_dim
        base_max_dim = TASK_DIMS.get(task_type, TASK_DIMS["default"])
        max_dim = _entropy_adaptive_dim(img_to_process, base_max_dim)

        # Stage 2: Resolution cap (token savings)
        if max(img_to_process.size) > max_dim:
            img_to_process.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        # Legacy size-based resize for very large estimated sizes
        img_bytes_approx = estimate_image_size_in_bytes(img_to_process)
        if img_bytes_approx > max_size_mb * 1024 * 1024:
            ratio = math.sqrt((max_size_mb * 1024 * 1024) / img_bytes_approx)
            new_dims = [int(dim * ratio) for dim in img_to_process.size]
            img_to_process = img_to_process.resize((new_dims[0], new_dims[1]), Image.Resampling.LANCZOS)

        # Stage 3: Format normalisation + JPEG compression
        if img_to_process.mode in ("RGBA", "P"):
            img_to_process = img_to_process.convert("RGB")

        buf = io.BytesIO()
        img_to_process.save(buf, format="JPEG", quality=85, optimize=True)
        result = buf.getvalue()

        # Stage 4: Fallback quality reduction if still too large
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


def _entropy_adaptive_dim(img: Image.Image, base_dim: int) -> int:
    """Adjust max image dimension based on Shannon entropy of the luminance channel.

    High entropy (> 6.5) = text-dense image (screenshot, document, code) → +50% resolution.
    Medium entropy (4.0–6.5) = mixed content → use base dimension.
    Low entropy (< 4.0) = simple photo, solid colors → -25% resolution (token savings).

    Computes entropy on a 256×256 thumbnail for speed (< 1ms).
    """
    try:
        # Downsample for fast entropy calculation (doesn't modify original)
        thumb = img.copy()
        thumb.thumbnail((256, 256), Image.Resampling.NEAREST)
        if thumb.mode != "L":
            thumb = thumb.convert("L")

        entropy = thumb.entropy()

        if entropy > 6.5:
            # High complexity: text, code, screenshots → boost resolution
            return int(base_dim * 1.5)
        elif entropy < 4.0:
            # Low complexity: simple photos, solid backgrounds → reduce resolution
            return int(base_dim * 0.75)
        else:
            return base_dim
    except Exception:
        # Fallback: if entropy calculation fails, use base dimension
        return base_dim


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
    global _image_pool_jobs
    if cache_key and cache_key in _compressed_cache:
        return _compressed_cache[cache_key]

    loop = asyncio.get_running_loop()
    retired_executor = None
    try:
        with _image_pool_lock:
            executor = _get_image_process_pool()
            if executor is not None:
                _cancel_image_idle_timer_locked()
                _image_pool_jobs += 1
        # Never hold the lifecycle lock across executor methods: broken-pool
        # callbacks run under its internal shutdown lock (opposite lock order).
        if executor is None:
            pending = loop.run_in_executor(None, _image_worker, image_data, max_size_mb, task_type)
        else:
            try:
                worker_future = executor.submit(_image_worker, image_data, max_size_mb, task_type)
            except BaseException:
                with _image_pool_lock:
                    if _detach_image_pool_locked(executor):
                        retired_executor = executor
                raise
            worker_future.add_done_callback(partial(_image_job_finished, executor))
            pending = asyncio.wrap_future(worker_future, loop=loop)
        result = await asyncio.wait_for(pending, timeout=timeout)
        if cache_key and result is not None:
            _compressed_cache[cache_key] = result
        return result
    except Exception as e:
        logging.error("Image processing error: %s", e, exc_info=True)
        return None
    finally:
        if retired_executor is not None:
            _close_image_executor(retired_executor)


def shutdown_image_pool() -> None:
    """Shut down the global image process pool (call during bot shutdown)."""
    with _image_pool_lock:
        pool = _image_process_pool
        _cancel_image_idle_timer_locked()
        detached = pool is not None and _detach_image_pool_locked(pool)
    if detached and pool is not None:
        _close_image_executor(pool)
        logging.info("Image process pool shut down")


def clear_image_cache() -> None:
    """Clear the compressed-image TTL cache (MemoryManager callback)."""
    count = len(_compressed_cache)
    _compressed_cache.clear()
    if count:
        logging.info("Cleared %d entries from compressed image cache", count)
