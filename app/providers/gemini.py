"""Google Gemini AI provider — self-contained execution logic."""

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Any, cast

import httpx
from cachetools import LRUCache
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image

from app.config import settings
from app.errors import ErrorCode, extract_retry_after_seconds, tag_error
from app.metrics import metrics_collector
from app.providers.base import (
    AIResponse,
    BaseAIProvider,
    _build_thinking_config,
)
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingMode,
    GroundingReport,
    GroundingSource,
    KeyDisposition,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    ThinkingLevel,
    TokenUsage,
    VisibleTextBuffer,
)
from app.providers.typed_payloads import gemini_contents
from app.utils.api_logger import api_logger
from app.utils.image_utils import TaggedImage, save_image_as_bytes

# Global cache for genai.Client instances to reuse connection pools (TLS/TCP)
# Key: API Key (string), Value: genai.Client
_gemini_clients_cache: LRUCache = LRUCache(maxsize=50)


class GeminiModelValidationStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


def _api_key_prefix(api_key: str) -> str:
    if api_key == "vertex":
        return "vertex"
    return api_key[:8]


def _optional_int_attribute(source: Any, name: str) -> int | None:
    value = getattr(source, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def get_cached_genai_client(api_key: str) -> genai.Client:
    """Return a cached genai.Client for the given API key, creating one if needed."""
    if api_key not in _gemini_clients_cache:
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        http_opts: dict[str, Any] = {"timeout": 90_000}
        client_kwargs["http_options"] = types.HttpOptions(**http_opts)  # type: ignore[arg-type]
        _gemini_clients_cache[api_key] = genai.Client(**client_kwargs)  # type: ignore[arg-type]
    return _gemini_clients_cache[api_key]


async def validate_gemini_chat_model_capability(
    model_name: str,
    *,
    api_keys: Iterable[str] | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> GeminiModelValidationStatus:
    """Check that a Gemini model exists and supports ``generateContent``.

    A definitive 404 or a model response without the chat capability is
    ``UNSUPPORTED``. Transport, auth, and quota errors are retried with the
    remaining configured keys and become ``UNAVAILABLE`` if none can answer.
    """
    keys = list(api_keys if api_keys is not None else settings.GEMINI_API_KEYS)
    if not keys:
        return GeminiModelValidationStatus.UNAVAILABLE

    make_client = client_factory or get_cached_genai_client
    saw_not_found = False
    for api_key in keys:
        try:
            model = await make_client(api_key).aio.models.get(model=model_name)
            actions = (
                getattr(model, "supported_actions", None)
                or getattr(model, "supported_generation_methods", None)
                or []
            )
            if "generateContent" in actions:
                return GeminiModelValidationStatus.SUPPORTED
            return GeminiModelValidationStatus.UNSUPPORTED
        except Exception as exc:
            code = getattr(exc, "code", None)
            error_text = str(exc).lower()
            if code == 404 or ("404" in error_text and "not found" in error_text):
                saw_not_found = True
                continue
            logging.getLogger(__name__).warning(
                "Gemini model capability check failed for key %s: %s",
                _api_key_prefix(api_key),
                type(exc).__name__,
            )

    if saw_not_found:
        return GeminiModelValidationStatus.UNSUPPORTED
    return GeminiModelValidationStatus.UNAVAILABLE


def get_live_api_client() -> "genai.Client | None":
    """Return a Gemini Developer API client for the Live API.

    gemini-3.1-flash-live-preview is a Gemini Developer API model — it is NOT
    available through Vertex AI.  This function returns a standard (non-Vertex)
    genai.Client using the first available GEMINI_API_KEY.

    Returns None if no Gemini API keys are configured.
    """
    keys = settings.GEMINI_API_KEYS
    if not keys:
        return None
    return get_cached_genai_client(keys[0])


# Vertex AI Express singleton — None if not configured or init failed.
_vertex_client: "genai.Client | None" = None
_vertex_client_initialized: bool = False
_vertex_live_client: "genai.Client | None" = None
_vertex_live_client_initialized: bool = False
_vertex_disabled_until_monotonic: float = 0.0
_VERTEX_CONFIG_ERROR_COOLDOWN_SECONDS = 3600.0
_VERTEX_CONFIG_ERROR_PATTERNS = (
    "BILLING_DISABLED",
    "requires billing to be enabled",
    "SERVICE_DISABLED",
    "PERMISSION_DENIED",
    "aiplatform.googleapis.com",
)


def get_vertex_client() -> "genai.Client | None":
    """Return a cached Vertex AI client, or None if not configured.

    Vertex AI Express Mode uses the same google-genai SDK but routes requests
    through GCP infrastructure.

    If VERTEX_AI_KEY is provided, it uses Express Mode (API key auth).
    Otherwise, it attempts to use ADC / service-account credentials.
    """
    global _vertex_client, _vertex_client_initialized
    if _vertex_client_initialized:
        return _vertex_client
    _vertex_client_initialized = True

    project = settings.VERTEX_AI_PROJECT
    location = settings.VERTEX_AI_LOCATION or "us-central1"
    api_key = settings.VERTEX_AI_KEY

    if not api_key and not project:
        return None  # Not configured — degrade gracefully

    log = logging.getLogger(__name__)
    try:
        http_opts: dict[str, Any] = {"timeout": 90_000}
        if api_key:
            # Express Mode: the API key already carries project/location metadata.
            # Passing project= or location= alongside api_key= causes the SDK to
            # switch to ADC mode and then fail with "credentials not found".
            _vertex_client = genai.Client(
                vertexai=True,
                api_key=api_key,
                http_options=types.HttpOptions(**http_opts),  # type: ignore[arg-type]
            )
            log.info("Vertex AI client initialized (Express Mode / API key)")
        else:
            # ADC / Service Account mode: relies on ambient credentials
            # (GOOGLE_APPLICATION_CREDENTIALS or GCP metadata server).
            _vertex_client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=types.HttpOptions(**http_opts),  # type: ignore[arg-type]
            )
            log.info(
                "Vertex AI client initialized (ADC mode, project=%s location=%s)",
                project,
                location,
            )
    except Exception as exc:
        log.warning("Vertex AI client init failed — Vertex AI pathway disabled: %s", exc)
        _vertex_client = None
    return _vertex_client


def is_vertex_client_available() -> bool:
    """Return True only when Vertex is configured and not in a local cooldown."""
    return get_vertex_client() is not None and time.monotonic() >= _vertex_disabled_until_monotonic


def report_vertex_error(exc: BaseException, *, cooldown_seconds: float = _VERTEX_CONFIG_ERROR_COOLDOWN_SECONDS) -> None:
    """Suppress repeated Vertex races after permanent configuration/billing errors."""
    global _vertex_disabled_until_monotonic

    error_text = str(exc)
    if not any(pattern in error_text for pattern in _VERTEX_CONFIG_ERROR_PATTERNS):
        return

    disabled_until = time.monotonic() + cooldown_seconds
    if disabled_until <= _vertex_disabled_until_monotonic:
        return

    _vertex_disabled_until_monotonic = disabled_until
    logging.getLogger(__name__).warning(
        "Vertex AI pathway disabled for %.0fs after configuration/billing error: %s",
        cooldown_seconds,
        error_text[:200],
    )


def get_vertex_live_client() -> "genai.Client | None":
    """Return a cached Vertex AI client for Live API sessions, or None if unavailable.

    Live API websocket sessions on Vertex are routed through the regional Vertex
    runtime and require a standard Vertex client configuration (project/location
    with ADC or service-account credentials). The Express API-key pathway used by
    non-live GenerateContent traffic is intentionally not reused here because it
    repeatedly fails with websocket 1007 "Invalid resource field value".
    """
    global _vertex_live_client, _vertex_live_client_initialized
    if _vertex_live_client_initialized:
        return _vertex_live_client
    _vertex_live_client_initialized = True

    project = settings.VERTEX_AI_PROJECT
    location = settings.VERTEX_AI_LOCATION or "us-central1"
    api_key = settings.VERTEX_AI_KEY
    log = logging.getLogger(__name__)
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

    if not project:
        if api_key:
            log.warning(
                "Vertex Live client disabled: Express API key is configured, "
                "but Live API requires a regional Vertex client with project/location credentials."
            )
        return None

    if credentials_path:
        if not os.path.exists(credentials_path):
            log.warning(
                "Vertex Live client disabled: GOOGLE_APPLICATION_CREDENTIALS path does not exist: %s",
                credentials_path,
            )
            return None
        if not os.access(credentials_path, os.R_OK):
            log.warning(
                "Vertex Live client disabled: GOOGLE_APPLICATION_CREDENTIALS path is not readable: %s",
                credentials_path,
            )
            return None

    try:
        http_opts: dict[str, Any] = {"timeout": 90_000, "api_version": "v1"}
        _vertex_live_client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=types.HttpOptions(**http_opts),  # type: ignore[arg-type]
        )
        log.info("Vertex Live client initialized (project=%s location=%s api_version=v1)", project, location)
    except Exception as exc:
        log.warning("Vertex Live client init failed — Live pathway disabled: %s", exc)
        _vertex_live_client = None
    return _vertex_live_client


class GeminiProvider(BaseAIProvider):
    """Google Gemini AI provider — self-contained execution logic."""

    provider_name = "gemini"

    def __init__(self, api_key: str):
        # We call super() first to validate the key type
        super().__init__(api_key)
        # Reuse cached client via shared factory
        self._client = get_cached_genai_client(api_key)
        self._client_api_key: str = api_key

    async def stream(self, request: GenerationRequest, *, model_name: str):
        """Emit typed Gemini stream events for one resolved key and model."""
        contents = await gemini_contents(request)
        route = RouteUsed(
            provider=(
                ProviderKind.VERTEX
                if self.provider_name == "gemini-vertex"
                else ProviderKind.GEMINI
            ),
            requested_model=request.models[0],
            actual_model=model_name,
        )
        if not contents:
            yield StreamFailed(
                code=ErrorCode.INVALID_REQUEST,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.DO_NOT_RETRY,
                key=KeyDisposition.UNCHANGED,
                diagnostic="Gemini content conversion produced no valid contents",
                route=route,
            )
            return

        config = types.GenerateContentConfig(safety_settings=settings.SAFETY_SETTINGS)  # type: ignore[arg-type]
        if request.grounding in {
            GroundingMode.PROVIDER_SEARCH,
            GroundingMode.PROVIDER_SEARCH_REQUIRED,
        }:
            config.tools = [types.Tool(google_search=types.GoogleSearch())]

        thinking_level = None
        if request.thinking_level is not None and request.thinking_level is not ThinkingLevel.AUTO:
            thinking_level = request.thinking_level.value
        thinking_config = _build_thinking_config(model_name, thinking_level)
        if thinking_config:
            config.thinking_config = thinking_config
        if request.system_instruction:
            config.system_instruction = request.system_instruction

        text_emitted = False
        finish_reason = FinishReason.from_raw(None)
        usage = TokenUsage()
        grounding = GroundingReport()
        text_buffer = VisibleTextBuffer()

        try:
            response_stream = await asyncio.wait_for(
                self._client.aio.models.generate_content_stream(
                    model=model_name,
                    contents=cast(Any, contents),
                    config=config,
                ),
                timeout=request.provider_timeout_seconds,
            )
            async for chunk in response_stream:
                candidates = getattr(chunk, "candidates", None) or []
                if candidates:
                    raw_finish = getattr(candidates[0], "finish_reason", None)
                    if raw_finish and str(raw_finish) != "FINISH_REASON_UNSPECIFIED":
                        finish_reason = FinishReason.from_raw(str(raw_finish))

                    metadata = getattr(candidates[0], "grounding_metadata", None)
                    grounding_chunks = getattr(metadata, "grounding_chunks", None) or []
                    sources: list[GroundingSource] = []
                    for grounding_chunk in grounding_chunks:
                        web = getattr(grounding_chunk, "web", None)
                        url = getattr(web, "uri", "") if web else ""
                        title = (getattr(web, "title", "") if web else "") or url
                        if url:
                            sources.append(GroundingSource(url=url, title=title))
                    if sources:
                        grounding = GroundingReport(sources=tuple(sources))

                native_usage = getattr(chunk, "usage_metadata", None)
                if native_usage is not None:
                    usage = TokenUsage(
                        prompt=_optional_int_attribute(native_usage, "prompt_token_count"),
                        completion=_optional_int_attribute(native_usage, "candidates_token_count"),
                        total=_optional_int_attribute(native_usage, "total_token_count"),
                        cached=_optional_int_attribute(native_usage, "cached_content_token_count"),
                    )

                text = getattr(chunk, "text", None)
                delta = text_buffer.push(text) if isinstance(text, str) else None
                if delta is not None:
                    text_emitted = True
                    yield delta

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            phase = FailurePhase.AFTER_TEXT if text_emitted else FailurePhase.BEFORE_TEXT
            message = str(exc)
            lowered = message.lower()
            retry = RetryDisposition.DO_NOT_RETRY if text_emitted else RetryDisposition.TRY_NEXT_KEY
            key = KeyDisposition.UNCHANGED if text_emitted else KeyDisposition.TRANSIENT_FAILURE
            if isinstance(exc, TimeoutError):
                code = ErrorCode.TIMEOUT
            elif "rate limit" in lowered or extract_retry_after_seconds(message) is not None:
                code = ErrorCode.RATE_LIMIT
                key = KeyDisposition.RATE_LIMITED
            elif "quota" in lowered:
                code = ErrorCode.QUOTA_EXCEEDED
                key = KeyDisposition.EXHAUSTED
            elif "api key" in lowered or "api_key_invalid" in lowered:
                code = ErrorCode.INVALID_KEY
                key = KeyDisposition.INVALID
            elif (
                getattr(exc, "code", None) in {503, 504}
                or "unavailable" in lowered
                or "overloaded" in lowered
            ):
                code = ErrorCode.OVERLOADED
            elif "invalid" in lowered or "malformed" in lowered:
                code = ErrorCode.INVALID_REQUEST
                retry = RetryDisposition.DO_NOT_RETRY
                key = KeyDisposition.UNCHANGED
            elif isinstance(exc, httpx.HTTPError):
                code = ErrorCode.NETWORK
            else:
                code = ErrorCode.GENERIC

            diagnostic = f"{type(exc).__name__}: {message}"[:500]
            if self.api_key:
                diagnostic = diagnostic.replace(self.api_key, "[redacted]")
            yield StreamFailed(
                code=code,
                phase=phase,
                retry=retry,
                key=key,
                diagnostic=diagnostic,
                route=route,
            )
            return

        if not text_emitted:
            yield StreamFailed(
                code=ErrorCode.EMPTY_RESPONSE,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="Gemini stream completed without visible text",
                route=route,
            )
            return

        yield StreamCompleted(
            finish_reason=finish_reason,
            usage=usage,
            grounding=grounding,
            route=route,
        )

    async def _execute_request(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        timeout: float,
        thinking_level: str | None = None,
        enable_web_search: bool = False,
        force_grounding: bool = False,
    ) -> AIResponse:
        start_time = None

        try:
            await metrics_collector.record_api_call("gemini", model_name)
            key_prefix = _api_key_prefix(self._client_api_key)

            # Compute metrics
            try:
                prompt_length = sum(
                    len(str(part)) for item in history for part in (item.get("parts", []) or []) if part is not None
                )
                has_images = any(
                    isinstance(part, (bytes, bytearray, Image.Image))
                    for item in history
                    for part in (item.get("parts", []) or [])
                    if part is not None
                )
            except Exception as e:
                logging.warning("Metrics calc error: %s", e)
                prompt_length = 0
                has_images = False

            start_time = api_logger.log_request(
                "gemini",
                model=model_name,
                key_prefix=key_prefix,
                prompt_length=prompt_length,
                has_images=has_images,
            )

            client = self._client

            # Convert history → types.Content
            contents = await self._build_contents(history)
            if contents is None:
                return self._error_response(
                    "Failed to create valid content for Gemini API",
                    model_name,
                    start_time,
                    user_id,
                    chat_id,
                )

            config = types.GenerateContentConfig(safety_settings=settings.SAFETY_SETTINGS)  # type: ignore[arg-type]  # Pydantic coerces dicts→SafetySetting
            # Apply Google Search Grounding if requested
            if enable_web_search:
                config.tools = [types.Tool(google_search=types.GoogleSearch())]

            # Apply thinking config if user requested a specific level
            tc = _build_thinking_config(model_name, thinking_level)
            if tc:
                config.thinking_config = tc
            if system_instruction:
                try:
                    config.system_instruction = str(system_instruction)
                except (TypeError, ValueError) as e:
                    logging.warning("Failed to set system_instruction: %s", e)

            # Native async call — properly supports CancelledError
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                ),
                timeout=timeout,
            )

            # Extract token count from response metadata (free, no extra API call).
            # Falls back to 0 if usage_metadata is unavailable.
            try:
                usage = getattr(response, "usage_metadata", None)
                token_count = getattr(usage, "total_token_count", 0) or getattr(usage, "candidates_token_count", 0) or 0
            except Exception as e:
                logging.debug("Token count from usage_metadata failed: %s", e)
                token_count = 0

            # Validate response
            if not response or not hasattr(response, "text"):
                return self._error_response(
                    "Gemini API returned invalid response object",
                    model_name,
                    start_time,
                    user_id,
                    chat_id,
                )

            response_text = response.text if response.text else ""
            if not response_text:
                # Inspect WHY the response is empty — safety block, prompt block, etc.
                block_reason = self._diagnose_empty_response(response)
                return self._error_response(
                    block_reason,
                    model_name,
                    start_time,
                    user_id,
                    chat_id,
                )

            # Log success
            if start_time is not None:
                api_logger.log_response(
                    "gemini",
                    start_time,
                    model=model_name,
                    key_prefix=key_prefix,
                    response_length=len(response_text),
                    token_count=token_count,
                )

            return AIResponse(
                text=response_text,
                token_count=token_count,
                success=True,
                provider=self.provider_name,
                model=model_name,
            )

        except TimeoutError:
            msg = f"Gemini API request timed out for model {model_name}"
            logging.error(msg)
            await metrics_collector.record_error("gemini_timeout", msg)
            self._log_failure(start_time, model_name, msg, user_id, chat_id)
            return AIResponse(
                text=tag_error(
                    ErrorCode.TIMEOUT,
                    "⏰ Превышено время ожидания ответа от API. Попробуйте позже.",
                ),
                token_count=0,
                success=False,
                error_message=msg,
                provider=self.provider_name,
                model=model_name,
            )

        except APIError as e:
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            logging.error("Gemini API Error: %s", e)
            err_lower = str(e).lower()
            status_code = getattr(e, "code", None)
            retry_after_seconds = extract_retry_after_seconds(str(e))

            if retry_after_seconds is not None:
                await metrics_collector.record_error("gemini_rate_limit", str(e))
                wait_text = (
                    f"⏱️ Временный лимит запросов модели. Подождите около {retry_after_seconds}с."
                    if retry_after_seconds
                    else "⏱️ Временный лимит запросов модели. Подождите немного."
                )
                text = tag_error(ErrorCode.RATE_LIMIT, wait_text)
            elif "quota" in err_lower:
                await metrics_collector.record_error("gemini_quota", str(e))
                text = tag_error(
                    ErrorCode.QUOTA_EXCEEDED,
                    "🚫 Достигнут лимит запросов к API (Quota Exceeded).",
                )
            elif (
                status_code in {503, 504}
                or "unavailable" in err_lower
                or "overloaded" in err_lower
                or "deadline_exceeded" in err_lower
            ):
                await metrics_collector.record_error("gemini_overloaded", str(e))
                raise  # Trigger retry in BaseAIProvider
            elif "api key" in err_lower or "api_key_invalid" in err_lower:
                await metrics_collector.record_error("gemini_invalid_key", str(e))
                text = tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ.")
            elif "invalid" in err_lower or "malformed" in err_lower:
                await metrics_collector.record_error("gemini_invalid_request", str(e))
                text = tag_error(
                    ErrorCode.INVALID_REQUEST,
                    "❌ Некорректный запрос к API. Проверьте параметры.",
                )
            elif "rate limit" in err_lower:
                await metrics_collector.record_error("gemini_rate_limit", str(e))
                text = tag_error(
                    ErrorCode.RATE_LIMIT,
                    "⏱️ Превышен лимит запросов в секунду. Подождите немного.",
                )
            else:
                await metrics_collector.record_error("gemini_api_call", str(e))
                text = tag_error(ErrorCode.GENERIC, f"Произошла ошибка вызова API: {e}")

            return AIResponse(
                text=text,
                token_count=0,
                success=False,
                error_message=str(e),
                provider=self.provider_name,
                model=model_name,
            )

        except httpx.HTTPError as e:
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            logging.error("Gemini HTTP error: %s", e, exc_info=True)
            await metrics_collector.record_error("gemini_http", str(e))
            return AIResponse(
                text=tag_error(ErrorCode.NETWORK, f"Произошла непредвиденная ошибка HTTP: {e}"),
                token_count=0,
                success=False,
                error_message=str(e),
                provider=self.provider_name,
                model=model_name,
            )

    async def _build_contents(self, history: list) -> list | None:
        """Convert history dicts → list[types.Content]. Returns None on total failure."""
        contents = []
        try:
            # Pass 1: Collect tasks for concurrent processing
            image_tasks = []
            for item in history:
                if not isinstance(item, dict):
                    continue
                parts = item.get("parts", [])
                if not isinstance(parts, list):
                    parts = [parts] if parts is not None else []

                for part in parts:
                    if isinstance(part, TaggedImage) and not part.pre_compressed:
                        image_tasks.append(
                            save_image_as_bytes(part.data, cache_key=part.cache_key, task_type=part.task_type)
                        )
                    elif isinstance(part, (bytes, bytearray, Image.Image)):
                        image_data = bytes(part) if isinstance(part, bytearray) else part
                        image_tasks.append(save_image_as_bytes(image_data))

            # Execute concurrently
            processed_images = []
            if image_tasks:
                processed_images = await asyncio.gather(*image_tasks, return_exceptions=True)

            # Pass 2: Build actual objects
            img_idx = 0
            for item in history:
                if not isinstance(item, dict):
                    logging.warning("Skipping invalid history item (not dict): %s", type(item))
                    continue
                role = item.get("role", "user")
                parts = item.get("parts", [])
                if not isinstance(parts, list):
                    parts = [parts] if parts is not None else []

                processed = []
                for part in parts:
                    img_bytes: bytes | None = None
                    if isinstance(part, TaggedImage):
                        if part.pre_compressed:
                            img_bytes = part.data
                        else:
                            res = processed_images[img_idx]
                            img_idx += 1
                            img_bytes = res if not isinstance(res, BaseException) else None

                        if img_bytes:
                            try:
                                processed.append(
                                    types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=img_bytes))
                                )
                            except (TypeError, ValueError) as e:
                                logging.warning("Failed to create image part: %s", e)
                        else:
                            logging.warning("Skipping TaggedImage part due to processing error")

                    elif isinstance(part, (bytes, bytearray, Image.Image)):
                        res = processed_images[img_idx]
                        img_idx += 1
                        img_bytes = res if not isinstance(res, BaseException) else None

                        if img_bytes:
                            try:
                                processed.append(
                                    types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=img_bytes))
                                )
                            except (TypeError, ValueError) as e:
                                logging.warning("Failed to create image part: %s", e)
                        else:
                            logging.warning("Skipping image part due to processing error")

                    else:
                        try:
                            processed.append(types.Part.from_text(text=str(part)))
                        except (TypeError, ValueError) as e:
                            logging.warning("Failed to process text part: %s", e)

                if processed:
                    try:
                        contents.append(types.Content(role=role, parts=processed))
                    except (TypeError, ValueError) as e:
                        logging.warning("Failed to create Content object: %s", e)

        except Exception as e:
            logging.error("Error processing history: %s", e, exc_info=True)
            return None

        return contents if contents else None

    def _diagnose_empty_response(self, response) -> str:
        """Inspect Gemini response to determine why text is empty."""
        # 1. Check prompt-level block (input was blocked)
        try:
            pf = getattr(response, "prompt_feedback", None)
            if pf:
                block_reason = getattr(pf, "block_reason", None)
                if block_reason:
                    logging.warning(
                        "Gemini prompt blocked: reason=%s, feedback=%s",
                        block_reason,
                        pf,
                    )
                    return "Запрос заблокирован фильтром безопасности Google. Попробуйте переформулировать сообщение."
        except Exception as e:
            logging.debug("prompt_feedback inspection error: %s", e)

        # 2. Check candidate-level finish reason
        try:
            candidates = getattr(response, "candidates", None)
            if candidates:
                candidate = candidates[0]
                finish_reason = getattr(candidate, "finish_reason", None)
                safety_ratings = getattr(candidate, "safety_ratings", None)

                if finish_reason and str(finish_reason).upper() in (
                    "SAFETY",
                    "2",
                    "FINISH_REASON_SAFETY",
                ):
                    ratings_str = ""
                    if safety_ratings:
                        ratings_str = ", ".join(
                            f"{getattr(r, 'category', '?')}={getattr(r, 'probability', '?')}" for r in safety_ratings
                        )
                    logging.warning(
                        "Gemini response safety-blocked: finish_reason=%s, ratings=[%s]",
                        finish_reason,
                        ratings_str,
                    )
                    return "Ответ заблокирован фильтром безопасности Google. Попробуйте переформулировать сообщение."

                if finish_reason and str(finish_reason).upper() in (
                    "MAX_TOKENS",
                    "3",
                    "FINISH_REASON_MAX_TOKENS",
                ):
                    logging.warning("Gemini response truncated: MAX_TOKENS")
                    return "Ответ превысил максимальную длину. Попробуйте более короткий запрос."

                if finish_reason and str(finish_reason).upper() in (
                    "RECITATION",
                    "4",
                    "FINISH_REASON_RECITATION",
                ):
                    logging.warning("Gemini response blocked: RECITATION")
                    return "Ответ заблокирован из-за совпадения с защищённым контентом."

                logging.warning(
                    "Gemini empty response with finish_reason=%s",
                    finish_reason,
                )
            else:
                logging.warning("Gemini response has no candidates")
        except Exception as e:
            logging.debug("candidate inspection error: %s", e)

        return "Gemini API вернул пустой ответ. Попробуйте ещё раз."

    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        if start_time is not None:
            api_logger.log_response(
                "gemini",
                start_time,
                model=model,
                key_prefix=_api_key_prefix(self._client_api_key),
                response_length=0,
                success=False,
                error_message=msg,
            )


# ── Vertex AI Provider ────────────────────────────────────────────────────────

_VERTEX_PLACEHOLDER_KEY = "vertex"  # Sentinel api_key for VertexGeminiProvider


class VertexGeminiProvider(GeminiProvider):
    """Vertex AI-backed Gemini provider.

    Uses the module-level Vertex AI client singleton (get_vertex_client()) instead
    of a per-key cached genai.Client.  The api_key parameter is a sentinel string
    ("vertex") that satisfies BaseAIProvider validation but is not used for auth.

    Intended as a supplementary race participant for models that experience 503
    storms on the Gemini Developer API while Vertex AI remains healthy.
    """

    provider_name = "gemini-vertex"

    def __init__(self) -> None:
        # BaseAIProvider.__init__ requires a non-empty string; use sentinel.
        super().__init__(_VERTEX_PLACEHOLDER_KEY)
        vertex_client = get_vertex_client()
        if vertex_client is None:
            raise RuntimeError("Vertex AI client is not configured")
        self._client = vertex_client
        self._client_api_key = _VERTEX_PLACEHOLDER_KEY
