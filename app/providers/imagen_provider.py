"""
Imagen 4 image generation provider.

Uses the google-genai SDK's `client.aio.models.generate_images()` endpoint.
Keys are selected from the same GEMINI_API_KEYS pool but tracked via an
*independent* in-memory RPD (requests-per-day) counter so that Imagen quota
exhaustion does NOT suspend keys for LLM chat or audio traffic.

Supported models (AI Studio / Gemini API free tier — 25 RPD each):
    imagen-4.0-fast-generate-001   — fastest, lowest latency
    imagen-4.0-generate-001        — balanced quality (default)
    imagen-4.0-ultra-generate-001  — highest quality, slowest
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from google.genai import types
from google.genai.errors import APIError

from app.config import (
    IMAGEN_MODEL_BASE,
    IMAGEN_MODELS_ORDERED,
    settings,
)
from app.providers.gemini import get_cached_genai_client

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Aspect ratios supported by Imagen 4 (Gemini API)
SUPPORTED_ASPECT_RATIOS: tuple[str, ...] = (
    "1:1",
    "3:4",
    "4:3",
    "9:16",
    "16:9",
)

# Human-readable labels for aspect ratio buttons
ASPECT_RATIO_LABELS: dict[str, str] = {
    "1:1": "◻️ 1:1",
    "3:4": "📱 3:4",
    "4:3": "🖥️ 4:3",
    "9:16": "📲 9:16",
    "16:9": "🎬 16:9",
}

# Short human-readable model labels for UI
MODEL_LABELS: dict[str, str] = {
    "imagen-4.0-fast-generate-001": "⚡ Fast",
    "imagen-4.0-generate-001": "✨ Base",
    "imagen-4.0-ultra-generate-001": "💎 Ultra",
}

# ---------------------------------------------------------------------------
# Per-key RPD budget tracker
#
# Primary:  Redis INCR + EXPIREAT(next UTC midnight) — survives bot restarts.
# Fallback: in-memory dict   — used transparently when Redis is unavailable
#           (dev environments, REDIS_URL not set, Redis connection errors).
#
# Redis key format:  imagen:rpd:<sha256(api_key)[:12]>
# ---------------------------------------------------------------------------

import datetime as _datetime
import hashlib as _hashlib

_KEY_DAY_BUCKET: dict[str, dict] = {}
_KEY_BUCKET_LOCK = asyncio.Lock()
_USER_DAY_BUCKET: dict[int, dict[str, float | int]] = {}
_USER_BUCKET_LOCK = asyncio.Lock()


def _day_bucket_key(api_key: str) -> str:
    """Short, stable, non-reversible identifier for an API key."""
    return _hashlib.sha256(api_key.encode()).hexdigest()[:12]


def _next_midnight_ts() -> int:
    """UNIX timestamp of the next UTC midnight (integer, for Redis EXPIREAT)."""
    now = _datetime.datetime.now(_datetime.UTC)
    tomorrow = (now + _datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(tomorrow.timestamp())


# --- Redis helpers ---


def _redis_imagen_key(api_key: str) -> str:
    return f"imagen:rpd:{_day_bucket_key(api_key)}"


async def _redis_get_usage(api_key: str) -> int | None:
    """Return usage count from Redis, or None if Redis is unavailable."""
    try:
        from app.cache import redis_client  # lazy import to avoid circular deps

        if redis_client is None:
            return None
        val = await redis_client.get(_redis_imagen_key(api_key))
        return int(val) if val is not None else 0
    except Exception as exc:
        logger.debug("Imagen Redis get failed (falling back to memory): %s", type(exc).__name__)
        return None


async def _redis_increment_usage(api_key: str) -> int | None:
    """Atomically increment Redis counter and set expiry. Returns new count or None."""
    try:
        from app.cache import redis_client

        if redis_client is None:
            return None
        rk = _redis_imagen_key(api_key)
        new_val: int = await redis_client.incr(rk)  # type: ignore[assignment]
        if new_val == 1:
            # First request today — set TTL to next UTC midnight
            await redis_client.expireat(rk, _next_midnight_ts())
        return new_val
    except Exception as exc:
        logger.debug("Imagen Redis incr failed (falling back to memory): %s", type(exc).__name__)
        return None


# --- Public interface (auto-selects Redis or in-memory) ---


async def _get_key_usage(api_key: str) -> int:
    """Return today's Imagen request count for the given key."""
    redis_result = await _redis_get_usage(api_key)
    if redis_result is not None:
        return redis_result

    # Fallback: in-memory
    async with _KEY_BUCKET_LOCK:
        bk = _day_bucket_key(api_key)
        entry = _KEY_DAY_BUCKET.get(bk)
        if entry is None:
            return 0
        if time.time() >= entry["reset_ts"]:
            _KEY_DAY_BUCKET[bk] = {"count": 0, "reset_ts": float(_next_midnight_ts())}
            return 0
        return entry["count"]


async def _increment_key_usage(api_key: str) -> int:
    """Increment today's counter (Redis preferred) and return the new count."""
    redis_result = await _redis_increment_usage(api_key)
    if redis_result is not None:
        return redis_result

    # Fallback: in-memory
    async with _KEY_BUCKET_LOCK:
        bk = _day_bucket_key(api_key)
        now = time.time()
        entry = _KEY_DAY_BUCKET.get(bk)
        if entry is None or now >= entry["reset_ts"]:
            _KEY_DAY_BUCKET[bk] = {"count": 1, "reset_ts": float(_next_midnight_ts())}
            return 1
        entry["count"] += 1
        return entry["count"]


async def _redis_consume_user_quota(user_id: int, limit: int) -> bool | None:
    """Atomically consume one user generation slot, or return None without Redis."""
    try:
        from app.cache import redis_client

        if redis_client is None:
            return None
        key = f"imagen:user_daily:{user_id}:{_datetime.datetime.now(_datetime.UTC):%Y%m%d}"
        new_value = int(await redis_client.incr(key))
        if new_value == 1:
            await redis_client.expireat(key, _next_midnight_ts())
        return new_value <= limit
    except Exception as exc:
        logger.debug("Imagen user quota Redis failure (falling back to memory): %s", type(exc).__name__)
        return None


async def _consume_user_daily_quota(user_id: int | None) -> bool:
    """Reserve one of the configured per-user Imagen generations for today."""
    limit = int(settings.IMAGE_GEN_DAILY_LIMIT)
    if user_id is None or limit <= 0:
        return True

    redis_result = await _redis_consume_user_quota(user_id, limit)
    if redis_result is not None:
        return redis_result

    async with _USER_BUCKET_LOCK:
        now = time.time()
        entry = _USER_DAY_BUCKET.get(user_id)
        if entry is None or now >= float(entry["reset_ts"]):
            _USER_DAY_BUCKET[user_id] = {"count": 1, "reset_ts": float(_next_midnight_ts())}
            return True
        current = int(entry["count"])
        if current >= limit:
            return False
        entry["count"] = current + 1
        return True


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ImageGenResult:
    """Result of a single image generation call."""

    success: bool
    images: list[bytes] = field(default_factory=list)  # raw PNG/JPEG bytes
    error_message: str = ""
    model_used: str = ""
    key_suffix: str = ""  # last 4 chars of API key (for debug logging)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ImagenProvider:
    """
    Generates images via the Imagen 4 API with automatic key rotation.

    Key selection strategy:
        1. Use the same GEMINI_API_KEYS pool (from settings).
        2. Skip any key that has reached IMAGE_GEN_RPD_PER_KEY today.
        3. On API error (quota / safety / overload), rotate to next key.
        4. On success, increment the per-key RPD counter.

    This approach ensures Imagen quota exhaustion NEVER touches the
    `key_status` table used by LLM/streaming, preventing cross-service
    impact.
    """

    async def generate(
        self,
        prompt: str,
        model: str = IMAGEN_MODEL_BASE,
        aspect_ratio: str = "1:1",
        number_of_images: int = 1,
        user_id: int | None = None,
    ) -> ImageGenResult:
        """
        Generate images from a text prompt.

        Args:
            prompt: Text description of the desired image.
            model: One of IMAGEN_MODELS_ORDERED.
            aspect_ratio: One of SUPPORTED_ASPECT_RATIOS.
            number_of_images: 1–4.

        Returns:
            ImageGenResult with raw image bytes on success.
        """
        if aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            aspect_ratio = "1:1"
        if model not in IMAGEN_MODELS_ORDERED:
            model = IMAGEN_MODEL_BASE
        number_of_images = max(1, min(4, number_of_images))

        keys: list[str] = list(settings.GEMINI_API_KEYS)
        if not keys:
            return ImageGenResult(success=False, error_message="no_keys")
        if not await _consume_user_daily_quota(user_id):
            logger.info("Imagen user daily limit reached: user=%s", user_id)
            return ImageGenResult(success=False, error_message="user_daily_limit")
        max_retries: int = min(settings.IMAGE_GEN_MAX_RETRIES, len(keys))

        last_error = ""
        for attempt in range(max_retries):
            # --- Key selection: pick key with budget remaining ---
            selected_key: str | None = None
            for key in keys:
                usage = await _get_key_usage(key)
                if usage < settings.IMAGE_GEN_RPD_PER_KEY:
                    selected_key = key
                    break

            if selected_key is None:
                logger.warning(
                    "Imagen: all %d keys exhausted for today (RPD limit=%d)",
                    len(keys),
                    settings.IMAGE_GEN_RPD_PER_KEY,
                )
                return ImageGenResult(
                    success=False,
                    error_message="quota_exhausted",
                )

            key_suffix = selected_key[-4:] if len(selected_key) >= 4 else "????"
            key_id = _day_bucket_key(selected_key)
            logger.info(
                "Imagen: attempt=%d/%d model=%s ratio=%s key_id=%s",
                attempt + 1,
                max_retries,
                model,
                aspect_ratio,
                key_id,
            )

            client = get_cached_genai_client(selected_key)

            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_images(
                        model=model,
                        prompt=prompt,
                        config=types.GenerateImagesConfig(
                            number_of_images=number_of_images,
                            aspect_ratio=aspect_ratio,
                            # Safety is already at BLOCK_NONE for text; Imagen
                            # has its own policy — we do not override here.
                        ),
                    ),
                    timeout=settings.IMAGE_GEN_TIMEOUT,
                )

                # Extract image bytes
                images_bytes: list[bytes] = []
                generated = getattr(response, "generated_images", None) or []
                for img_obj in generated:
                    image_data = getattr(img_obj, "image", None)
                    if image_data is not None:
                        raw_bytes = getattr(image_data, "image_bytes", None)
                        if raw_bytes:
                            images_bytes.append(bytes(raw_bytes))

                if not images_bytes:
                    logger.warning(
                        "Imagen: response contained no images (model=%s key_id=%s)",
                        model,
                        key_id,
                    )
                    # Rotate key in case this is a silent API refusal
                    keys = [k for k in keys if k != selected_key]
                    last_error = "empty_response"
                    continue

                await _increment_key_usage(selected_key)
                logger.info(
                    "Imagen: success — %d image(s) generated (model=%s key_id=%s)",
                    len(images_bytes),
                    model,
                    key_id,
                )
                return ImageGenResult(
                    success=True,
                    images=images_bytes,
                    model_used=model,
                    key_suffix=key_suffix,
                )

            except TimeoutError:
                logger.error(
                    "Imagen: timeout after %.0fs (model=%s key_id=%s)",
                    settings.IMAGE_GEN_TIMEOUT,
                    model,
                    key_id,
                )
                last_error = "timeout"
                keys = [k for k in keys if k != selected_key]

            except APIError as exc:
                err_lower = str(exc).lower()
                logger.error("Imagen: APIError (key_id=%s)", key_id)

                if "paid plan" in err_lower or "limit: 0" in err_lower:
                    # Google disabled Imagen on Free Tier or requires billing
                    return ImageGenResult(
                        success=False,
                        error_message="paid_tier_required",
                        model_used=model,
                        key_suffix=key_suffix,
                    )
                elif "quota" in err_lower or "resource_exhausted" in err_lower or "429" in err_lower:
                    # Count this key as depleted for the day
                    await _increment_key_usage(selected_key)
                    last_error = "quota"
                    keys = [k for k in keys if k != selected_key]

                elif "safety" in err_lower or "block" in err_lower or "400" in err_lower:
                    # Safety block is not a key problem — fail fast, no rotation
                    return ImageGenResult(
                        success=False,
                        error_message="safety_blocked",
                        model_used=model,
                        key_suffix=key_suffix,
                    )

                elif "503" in err_lower or "unavailable" in err_lower or "overloaded" in err_lower:
                    last_error = "overloaded"
                    keys = [k for k in keys if k != selected_key]

                else:
                    last_error = f"api_error:{type(exc).__name__}"
                    keys = [k for k in keys if k != selected_key]

            except Exception as exc:
                error_type = type(exc).__name__
                logger.error("Imagen: unexpected error (key_id=%s, error_type=%s)", key_id, error_type)
                last_error = f"unexpected:{error_type}"
                keys = [k for k in keys if k != selected_key]

            if not keys:
                break

        return ImageGenResult(
            success=False,
            error_message=last_error or "all_keys_failed",
        )


# Module-level singleton
_imagen_provider: ImagenProvider | None = None


def get_imagen_provider() -> ImagenProvider:
    """Return the singleton ImagenProvider."""
    global _imagen_provider
    if _imagen_provider is None:
        _imagen_provider = ImagenProvider()
    return _imagen_provider
