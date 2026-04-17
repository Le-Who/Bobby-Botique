"""
Pollinations.ai image generation provider.

Transport:
    Primary:  POST /v1/images/generations  — OpenAI-compatible, returns JSON url/b64.
              Requires an API key for best rate limits (optional, works without key too).
    Fallback: GET  /image/{prompt}?model=… — keyless, returns raw image bytes directly.

The provider uses `httpx` (already in requirements.txt) for async HTTP and performs
Content-Type validation on the GET fallback to prevent returning an HTML error page
as an "image".

Supported free models (no key required):
    flux    — Flux Schnell, fast high-quality generation.
    zimage  — Z-Image Turbo, 6B Flux with 2× upscaling.

Additional models (paid or with key):
    gptimage, gptimage-large, kontext, klein, seedream5, grok-imagine, …

Usage:
    provider = get_pollinations_provider()
    result   = await provider.generate("a cat in space", model="flux")
    transcript = await provider.transcribe_audio(audio_bytes)
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from app.config import POLLINATIONS_BASE_URL, settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Human-readable labels for Pollinations models (used in Telegram UI)
# New models that arrive via env are auto-labeled via _make_label().
# ---------------------------------------------------------------------------

_KNOWN_LABELS: dict[str, str] = {
    "flux": "✨ Flux (Универсальная)",
    "zimage": "⚡ Z-Image (Быстрая)",
    "gptimage": "🤖 DALL-E 3 (Точная)",
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
    """
    Async image generation via Pollinations.ai.

    Key selection strategy:
        1. POST /v1/images/generations with optional Bearer token.
           Returns JSON {"data": [{"url": "..."} | {"b64_json": "..."}]}.
        2. On any failure (timeout / non-2xx / parse error), falls back to
           GET /image/{encoded_prompt}?model=…&seed=0&enhance=false .
           Response must have Content-Type: image/* to be accepted.

    Both paths respect settings.IMAGE_GEN_TIMEOUT.
    """

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

        # Validate model falls back to flux if unknown
        available = settings.POLLINATIONS_IMAGE_MODELS
        if model not in available:
            logger.warning("Pollinations: unknown model %r, falling back to flux", model)
            model = settings.POLLINATIONS_DEFAULT_IMAGE_MODEL or "flux"

        timeout = settings.IMAGE_GEN_TIMEOUT

        # --- Primary: POST endpoint ---
        result = await self._try_post(
            prompt=prompt,
            model=model,
            width=width,
            height=height,
            seed=seed,
            enhance=enhance,
            negative_prompt=negative_prompt,
            timeout=timeout,
        )

        if result.success:
            return result

        # Log the POST failure but attempt GET fallback transparently
        logger.info("Pollinations: POST failed (%s), trying GET fallback", result.error_message)

        get_result = await self._try_get(
            prompt=prompt,
            model=model,
            width=width,
            height=height,
            seed=seed,
            enhance=enhance,
            negative_prompt=negative_prompt,
            timeout=timeout,
        )

        if get_result.success:
            get_result.warning = "used GET fallback"

        return get_result

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
        url = "https://text.pollinations.ai/openai/audio/transcriptions"
        
        # httpx expects files in format: {'file': ('filename', b'content', 'mime_type')}
        files: dict[str, tuple[str, bytes, str]] = {
            "file": ("audio.ogg", audio_bytes, "audio/ogg")
        }
        data: dict[str, str] = {
            "model": model,
            "response_format": "text"
        }
        
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, data=data, files=files)
                
            if 200 <= resp.status_code < 300:
                content_type = resp.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    try:
                        js = resp.json()
                        if "choices" in js and isinstance(js["choices"], list):
                            logger.error("Pollinations whisper hallucinated a chat response.")
                            return None
                        return js.get("text", "").strip() or None
                    except Exception as e:
                        logger.warning("Pollinations whisper JSON parse error: %s", e)
                        return None
                else:
                    text_res = resp.text.strip()
                    if not text_res or text_res.startswith(('{"id":', '{"choices":')):
                        logger.error("Pollinations whisper returned stringified JSON payload instead of text.")
                        return None
                    return text_res
            else:
                logger.warning("Pollinations whisper error: %d - %s", resp.status_code, resp.text[:200])
                return None
        except httpx.TimeoutException:
            logger.warning("Pollinations whisper request timed out after %s s", timeout)
            return None
        except Exception as exc:
            logger.error("Pollinations whisper request failed: %s", exc)
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
        api_key = settings.POLLINATIONS_API_KEY
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
            logger.debug("Pollinations POST error: %s", exc)
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

        api_key = settings.POLLINATIONS_API_KEY
        if api_key:
            params["key"] = api_key

        url = f"{POLLINATIONS_BASE_URL}/image/{encoded_prompt}"

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, params=params)

            if not (200 <= resp.status_code < 300):
                return PollinationsResult(
                    success=False,
                    error_message=f"get_http_{resp.status_code}",
                    model_used=model,
                )

            # Safety: must be an image (guards against CloudFlare HTML error pages)
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                logger.warning("Pollinations GET: unexpected content-type %r (not image/*)", content_type)
                return PollinationsResult(
                    success=False,
                    error_message="invalid_content_type",
                    model_used=model,
                )

            image_bytes = resp.content
            if not image_bytes:
                return PollinationsResult(success=False, error_message="empty_response", model_used=model)

            logger.info(
                "Pollinations GET fallback: success — model=%s size=%dx%d bytes=%d",
                model,
                width,
                height,
                len(image_bytes),
            )
            return PollinationsResult(success=True, images=[image_bytes], model_used=model)

        except httpx.TimeoutException:
            return PollinationsResult(success=False, error_message="timeout", model_used=model)
        except Exception as exc:
            logger.error("Pollinations GET error: %s", exc, exc_info=True)
            return PollinationsResult(
                success=False,
                error_message=f"get_error:{type(exc).__name__}",
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
            try:
                result.append(base64.b64decode(b64))
                continue
            except Exception as exc:
                logger.warning("Pollinations: failed to decode b64_json: %s", exc)

        url_str = item.get("url")
        if url_str:
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    fetched = await client.get(url_str)
                content_type = fetched.headers.get("content-type", "")
                if fetched.status_code == 200 and content_type.startswith("image/"):
                    result.append(fetched.content)
            except Exception as exc:
                logger.warning("Pollinations: failed to fetch url %s: %s", url_str, exc)

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
