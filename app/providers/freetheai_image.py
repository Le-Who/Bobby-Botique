"""FreeTheAI image generation provider.

Uses ``https://api.freetheai.xyz/v1/images/generations`` with Bearer auth.

Supported models:
    vhr/gpt_image_2          — GPT Image 2 via FTA
    vhr/nano_banana_2        — NanoBanana v2
    vhr/bytedance_seedream_v4 — ByteDance SeedReam v4

Response may contain either ``b64_json`` or ``url`` — both are handled.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_freetheai_keys

logger = logging.getLogger(__name__)

# FTA image models that can be selected by the user.
FTA_IMAGE_MODELS: list[str] = [
    "vhr/gpt_image_2",
    "img/gpt-image-2",
    "vhr/nano_banana_2",
    "vhr/bytedance_seedream_v4",
]
FTA_IMAGE_DEFAULT: str = "vhr/gpt_image_2"

# Human-readable labels for UI
FTA_IMAGE_MODEL_LABELS: dict[str, str] = {
    "vhr/gpt_image_2": "🎨 GPT Image 2 (vhr)",
    "img/gpt-image-2": "🖼️ GPT Image 2 (img)",
    "vhr/nano_banana_2": "🍌 NanoBanana 2",
    "vhr/bytedance_seedream_v4": "🌀 SeedReam v4",
}

_FTA_IMAGES_URL = "https://api.freetheai.xyz/v1/images/generations"
_FTA_EDITS_URL = "https://api.freetheai.xyz/v1/images/edits"
_FTA_IMAGE_TIMEOUT = 180.0  # generous — image models can be slow

# In-memory key health (same pattern as freetheai chat)
_fta_img_key_health: dict[str, datetime] = {}
_FTA_IMG_COOLDOWN = timedelta(seconds=60)


@dataclass
class FTAImageResult:
    """Result of a FreeTheAI image generation call."""

    success: bool
    images: list[bytes] = field(default_factory=list)  # raw image bytes
    error_message: str = ""
    model_used: str = ""
    key_suffix: str = ""


def _suspend_fta_img_key(key_hash: str, cooldown: timedelta | None = None) -> None:
    # With a single key, suspending it means total unavailability — skip.
    if len(get_freetheai_keys()) <= 1:
        logger.info("FTA Image: single-key mode, skipping suspension for %s…", key_hash[:8])
        return
    _fta_img_key_health[key_hash] = datetime.now(UTC) + (cooldown or _FTA_IMG_COOLDOWN)
    logger.warning(
        "FTA Image key %s… suspended for %.0fs",
        key_hash[:8],
        (cooldown or _FTA_IMG_COOLDOWN).total_seconds(),
    )


def _pick_key() -> tuple[str, str] | None:
    """Pick a healthy FreeTheAI key. Returns (api_key, key_hash) or None."""
    keys = get_freetheai_keys()
    if not keys:
        return None
    now = datetime.now(UTC)
    for key in keys:
        kh = hashlib.sha256(key.encode()).hexdigest()[:16]
        suspended_until = _fta_img_key_health.get(kh)
        if suspended_until and now < suspended_until:
            continue
        return key, kh
    # All suspended — clean expired
    expired = [h for h, until in list(_fta_img_key_health.items()) if now >= until]
    for h in expired:
        del _fta_img_key_health[h]
    return None


class FreeTheAIImageProvider:
    """Generates images via FreeTheAI's /v1/images/generations endpoint."""

    async def generate(
        self,
        prompt: str,
        model: str = FTA_IMAGE_DEFAULT,
        *,
        size: str | None = None,
        quality: str | None = None,
        image_base64: str | None = None,
    ) -> FTAImageResult:
        """Generate or edit an image from a text prompt.

        Args:
            prompt: Text description of the desired image.
            model: One of FTA_IMAGE_MODELS.
            size: Optional size string (e.g. "1024x1024").
            quality: Optional quality string (e.g. "hd").
            image_base64: Optional base64 encoded image string for /edits.

        Returns:
            FTAImageResult with raw image bytes on success.
        """
        if model not in FTA_IMAGE_MODELS:
            model = FTA_IMAGE_DEFAULT

        key_pair = _pick_key()
        if key_pair is None:
            logger.warning("FTA Image: no FreeTheAI keys available")
            return FTAImageResult(
                success=False,
                error_message="no_keys",
            )

        api_key, key_hash = key_pair
        key_suffix = api_key[-4:] if len(api_key) >= 4 else "????"

        payload: dict = {
            "model": model,
            "prompt": prompt,
        }
        if size:
            payload["size"] = size
        if quality:
            payload["quality"] = quality
            
        endpoint_url = _FTA_IMAGES_URL
        if image_base64:
            payload["image"] = image_base64
            endpoint_url = _FTA_EDITS_URL

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "FTA Image: model=%s key=…%s prompt_len=%d",
            model,
            key_suffix,
            len(prompt),
        )

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_FTA_IMAGE_TIMEOUT) as client:
                response = await client.post(endpoint_url, json=payload, headers=headers)

            elapsed = time.monotonic() - t0

            if response.status_code != 200:
                body = response.text[:500]
                logger.error(
                    "FTA Image: HTTP %d after %.1fs (model=%s key=…%s): %s",
                    response.status_code,
                    elapsed,
                    model,
                    key_suffix,
                    body,
                )
                if response.status_code == 429:
                    _suspend_fta_img_key(key_hash, timedelta(seconds=120))
                    return FTAImageResult(success=False, error_message="rate_limited", model_used=model, key_suffix=key_suffix)
                if response.status_code in (401, 403):
                    _suspend_fta_img_key(key_hash, timedelta(minutes=30))
                    return FTAImageResult(success=False, error_message="auth_error", model_used=model, key_suffix=key_suffix)
                return FTAImageResult(success=False, error_message=f"http_{response.status_code}", model_used=model, key_suffix=key_suffix)

            data = response.json()
            images_bytes: list[bytes] = []
            items = data.get("data") or []

            for item in items:
                # Response may have b64_json or url
                b64 = item.get("b64_json")
                if b64:
                    images_bytes.append(base64.b64decode(b64))
                    continue
                url = item.get("url")
                if url:
                    try:
                        async with httpx.AsyncClient(timeout=120) as dl_client:
                            dl_resp = await dl_client.get(url)
                            dl_resp.raise_for_status()
                            images_bytes.append(dl_resp.content)
                    except Exception as dl_exc:
                        logger.warning("FTA Image: failed to download url %s: %s", url[:100], dl_exc)

            if not images_bytes:
                logger.warning("FTA Image: response contained no images (model=%s)", model)
                return FTAImageResult(success=False, error_message="empty_response", model_used=model, key_suffix=key_suffix)

            logger.info(
                "FTA Image: success — %d image(s) in %.1fs (model=%s key=…%s)",
                len(images_bytes),
                elapsed,
                model,
                key_suffix,
            )
            return FTAImageResult(
                success=True,
                images=images_bytes,
                model_used=model,
                key_suffix=key_suffix,
            )

        except TimeoutError:
            logger.error("FTA Image: timeout after %.0fs (model=%s)", _FTA_IMAGE_TIMEOUT, model)
            return FTAImageResult(success=False, error_message="timeout", model_used=model, key_suffix=key_suffix)
        except Exception as exc:
            logger.error("FTA Image: unexpected error: %s", exc, exc_info=True)
            return FTAImageResult(success=False, error_message=f"unexpected:{exc}", model_used=model, key_suffix=key_suffix)


# Module-level singleton
_fta_image_provider: FreeTheAIImageProvider | None = None


def get_fta_image_provider() -> FreeTheAIImageProvider:
    """Return the singleton FreeTheAIImageProvider."""
    global _fta_image_provider
    if _fta_image_provider is None:
        _fta_image_provider = FreeTheAIImageProvider()
    return _fta_image_provider
