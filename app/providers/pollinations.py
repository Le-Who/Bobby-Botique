"""Authenticated Pollinations generation using the current gen.pollinations.ai API."""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from app.config import POLLINATIONS_BASE_URL, settings
from app.utils.media_download import (
    IMAGE_MIME_TYPES,
    MAX_IMAGE_DOWNLOAD_BYTES,
    MediaDownloadError,
    download_media,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def fetch_models(kind: str) -> list[dict]:
    """Read the public catalog, retaining canonical IDs and legacy aliases."""
    if kind not in {"image", "text"}:
        raise ValueError("Unsupported catalog kind")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{POLLINATIONS_BASE_URL}/{kind}/models")
        response.raise_for_status()
    payload = response.json()
    entries = payload.get("data", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("Invalid Pollinations catalog")
    endpoint = "/v1/images/generations" if kind == "image" else "/v1/chat/completions"
    models = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        model_id = item.get("name") or item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if kind not in item.get("output_modalities", [kind]):
            continue
        if endpoint not in item.get("supported_endpoints", [endpoint]):
            continue
        models.append(
            {
                "id": model_id,
                "title": item.get("title") or model_id,
                "aliases": [alias for alias in item.get("aliases", []) if isinstance(alias, str)],
                "publisher": item.get("publisher", ""),
            }
        )
    return models


# ---------------------------------------------------------------------------
# Human-readable labels for Pollinations models (used in Telegram UI)
# New models that arrive via env are auto-labeled via _make_label().
# ---------------------------------------------------------------------------

_KNOWN_LABELS: dict[str, str] = {
    "flux": "✨ Flux (Универсальная)",
    "zimage": "⚡ Z-Image (Быстрая)",
    "gptimage": "🤖 GPT Image (Точная)",
    "gptimage-large": "💎 GPT Image HD",
    "kontext": "🖋️ Kontext",
    "klein": "🎨 Klein (Креативная)",
    "seedream5": "🌱 Seedream 5",
    "grok-imagine": "🚀 Grok",
    "grok-imagine-pro": "💠 Grok Pro",
    "p-image": "🟣 p-image",
    "nova-canvas": "☁️ Nova Canvas",
    "nanobanana": "🍌 NanoBanana",
    "nanobanana-2": "🍌² NanoBanana 2",
    "qwen-image": "🌏 Qwen (Аниме/Арт)",
    "wan-image": "🌟 Wan (Реализм)",
}


def get_model_label(model_id: str) -> str:
    """Return a human-readable label for a model id, auto-generating for unknowns."""
    if model_id in _KNOWN_LABELS:
        return _KNOWN_LABELS[model_id]
    # Auto-generate: capitalize words, strip hyphens, add generic icon
    pretty = " ".join(w.capitalize() for w in model_id.replace("-", " ").split())
    return f"🎨 {pretty}"


# ---------------------------------------------------------------------------
# Result dataclass  (mirrors ImageGenResult from imagen_provider for compatibility)
# ---------------------------------------------------------------------------


@dataclass
class PollinationsResult:
    """Result of a single Pollinations image generation call."""

    success: bool
    images: list[bytes] = field(default_factory=list)  # raw JPEG/PNG bytes
    error_message: str = ""
    model_used: str = ""
    warning: str = ""  # non-fatal info (e.g., "used GET fallback")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class PollinationsProvider:
    """Async authenticated images and transcription; no anonymous fallback."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        model: str = "flux",
        width: int = 1024,
        height: int = 1024,
        seed: int = 0,
        enhance: bool = False,
        negative_prompt: str = "",
    ) -> PollinationsResult:
        """
        Generate a single image.

        Args:
            prompt:          Text description.
            model:           Pollinations model id (e.g. "flux", "zimage").
            width/height:    Output dimensions in pixels.
            seed:            Reproducibility seed. 0 = deterministic, -1 = random.
            enhance:         Whether Pollinations should auto-enhance the prompt.
            negative_prompt: Negative prompt (supported by flux, zimage).

        Returns:
            PollinationsResult with image bytes on success.
        """
        if not prompt or not prompt.strip():
            return PollinationsResult(success=False, error_message="empty_prompt")

        from app.repos.provider_keys import get_provider_key

        if not await get_provider_key("pollinations"):
            return PollinationsResult(success=False, error_message="unauthorized", model_used=model)
        # Canonical publisher/model IDs and saved aliases are accepted by the API.
        # Do not replace an explicit choice with a different (potentially paid) model.
        return await self._try_post(
            prompt=prompt,
            model=model,
            width=width,
            height=height,
            seed=seed,
            enhance=enhance,
            negative_prompt=negative_prompt,
            timeout=settings.IMAGE_GEN_TIMEOUT,
        )

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        model: str = "whisper",
        timeout: float = 60.0,
    ) -> str | None:
        """
        Transcribe audio using Pollinations OpenAI-compatible endpoint.

        Args:
            audio_bytes: Raw audio bytes (OGG, MP3, WAV).
            model: "whisper" or "whisper-large".
            timeout: Request timeout.

        Returns:
            Transcribed text or None on failure.
        """
        url = f"{POLLINATIONS_BASE_URL}/v1/audio/transcriptions"
        from app.repos.provider_keys import get_provider_key

        api_key = await get_provider_key("pollinations")
        if not api_key:
            return None
        headers = {"Authorization": f"Bearer {api_key}"}

        # httpx expects files in format: {'file': ('filename', b'content', 'mime_type')}
        files: dict[str, tuple[str, bytes, str]] = {"file": ("audio.ogg", audio_bytes, "audio/ogg")}
        data: dict[str, str] = {"model": model, "response_format": "text"}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, data=data, files=files, headers=headers)

            if 200 <= resp.status_code < 300:
                content_type = resp.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    try:
                        js = resp.json()
                        if "choices" in js and isinstance(js["choices"], list):
                            logger.error("Pollinations whisper hallucinated a chat response.")
                            return None
                        return js.get("text", "").strip() or None
                    except Exception as exc:
                        logger.warning("Pollinations whisper JSON parse error (%s)", type(exc).__name__)
                        return None
                else:
                    text_res = resp.text.strip()
                    if not text_res or text_res.startswith(('{"id":', '{"choices":')):
                        logger.error("Pollinations whisper returned stringified JSON payload instead of text.")
                        return None
                    return text_res
            else:
                logger.warning("Pollinations whisper HTTP error: %d", resp.status_code)
                return None
        except httpx.TimeoutException:
            logger.warning("Pollinations whisper request timed out after %s s", timeout)
            return None
        except Exception as exc:
            logger.error("Pollinations whisper request failed (error_type=%s)", type(exc).__name__)
            return None

    # ------------------------------------------------------------------
    # Private: POST path
    # ------------------------------------------------------------------

    async def _try_post(
        self,
        prompt: str,
        model: str,
        width: int,
        height: int,
        seed: int,
        enhance: bool,
        negative_prompt: str,
        timeout: float,
    ) -> PollinationsResult:
        url = f"{POLLINATIONS_BASE_URL}/v1/images/generations"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        from app.repos.provider_keys import get_provider_key

        api_key = await get_provider_key("pollinations")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict = {
            "prompt": prompt,
            "model": model,
            "width": width,
            "height": height,
            "size": f"{width}x{height}",
            "response_format": "b64_json",
            "seed": seed,
            "enhance": enhance,
            "nologo": True,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 402:
                return PollinationsResult(success=False, error_message="paid_tier_required", model_used=model)
            if resp.status_code in (401, 403):
                return PollinationsResult(success=False, error_message="unauthorized", model_used=model)
            if not (200 <= resp.status_code < 300):
                return PollinationsResult(
                    success=False,
                    error_message=f"http_{resp.status_code}",
                    model_used=model,
                )

            data = resp.json()
            images_bytes = await _extract_b64_or_url_bytes(data, timeout=timeout)

            if not images_bytes:
                return PollinationsResult(success=False, error_message="empty_response", model_used=model)

            logger.info("Pollinations POST: success — model=%s size=%dx%d", model, width, height)
            return PollinationsResult(success=True, images=images_bytes, model_used=model)

        except httpx.TimeoutException:
            return PollinationsResult(success=False, error_message="timeout", model_used=model)
        except Exception as exc:
            logger.debug("Pollinations POST error (error_type=%s)", type(exc).__name__)
            return PollinationsResult(
                success=False,
                error_message=f"post_error:{type(exc).__name__}",
                model_used=model,
            )

    # ------------------------------------------------------------------
    # Private: GET fallback path
    # ------------------------------------------------------------------

    async def _try_get(
        self,
        prompt: str,
        model: str,
        width: int,
        height: int,
        seed: int,
        enhance: bool,
        negative_prompt: str,
        timeout: float,
    ) -> PollinationsResult:
        encoded_prompt = urllib.parse.quote(prompt, safe="")
        params: dict = {
            "model": model,
            "width": width,
            "height": height,
            "seed": seed,
            "enhance": str(enhance).lower(),
            "safe": "false",
            "nologo": "true",
        }
        if negative_prompt:
            params["negative_prompt"] = negative_prompt

        from app.repos.provider_keys import get_provider_key

        api_key = await get_provider_key("pollinations")
        if not api_key:
            return PollinationsResult(success=False, error_message="unauthorized", model_used=model)

        url = f"{POLLINATIONS_BASE_URL}/image/{encoded_prompt}"

        try:
            request_url = str(httpx.URL(url, params=params))
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}) as client:
                image_bytes = await download_media(
                    request_url,
                    allowed_mime_types=IMAGE_MIME_TYPES,
                    max_bytes=MAX_IMAGE_DOWNLOAD_BYTES,
                    timeout=timeout,
                    client=client,
                    # Never forward generation credentials to a redirect target.
                    max_redirects=0,
                )

            logger.info(
                "Pollinations GET fallback: success — model=%s size=%dx%d bytes=%d",
                model,
                width,
                height,
                len(image_bytes),
            )
            return PollinationsResult(success=True, images=[image_bytes], model_used=model)

        except MediaDownloadError as exc:
            logger.warning("Pollinations GET download failed (%s)", exc.code)
            return PollinationsResult(
                success=False,
                error_message=f"get_error:{exc.code}",
                model_used=model,
            )


# ---------------------------------------------------------------------------
# Helper: extract image bytes from POST response body
# ---------------------------------------------------------------------------


async def _extract_b64_or_url_bytes(data: dict, timeout: float = 30.0) -> list[bytes]:
    """
    Parse the OpenAI-compatible response body.

    Handles both:
      {"data": [{"b64_json": "..."}]}
      {"data": [{"url": "https://..."}]}    ← rare; we fetch inline
    """
    import base64

    items: list = data.get("data") or []
    result: list[bytes] = []

    for item in items:
        b64 = item.get("b64_json")
        if b64:
            max_encoded_chars = ((MAX_IMAGE_DOWNLOAD_BYTES + 2) // 3) * 4
            if not isinstance(b64, str) or len(b64) > max_encoded_chars:
                logger.warning("Pollinations: rejected oversized b64_json")
                continue
            try:
                decoded = base64.b64decode(b64, validate=True)
                if len(decoded) > MAX_IMAGE_DOWNLOAD_BYTES:
                    logger.warning("Pollinations: rejected oversized decoded image")
                    continue
                result.append(decoded)
                continue
            except Exception:
                logger.warning("Pollinations: failed to decode b64_json")

        url_str = item.get("url")
        if url_str:
            try:
                result.append(
                    await download_media(
                        url_str,
                        allowed_mime_types=IMAGE_MIME_TYPES,
                        max_bytes=MAX_IMAGE_DOWNLOAD_BYTES,
                        timeout=timeout,
                    )
                )
            except MediaDownloadError as exc:
                logger.warning("Pollinations: media download failed (%s)", exc.code)

    return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_pollinations_provider: PollinationsProvider | None = None


def get_pollinations_provider() -> PollinationsProvider:
    """Return the singleton PollinationsProvider."""
    global _pollinations_provider
    if _pollinations_provider is None:
        _pollinations_provider = PollinationsProvider()
    return _pollinations_provider
