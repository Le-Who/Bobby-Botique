"""
ProviderRouter — routes AI requests with key rotation and health scoring.

Also provides the module-level convenience functions:
- get_provider_router() → singleton
- get_ai_response()     → backward-compat wrapper
"""

import asyncio
import logging
from typing import Any

from app.config import (
    CURRENT_GEMINI_MODELS,
    DEFAULT_GEMINI_MODELS,
    GEMINI_ECONOMY_MODEL,
    GEMINI_GROUNDING_FALLBACK_MODEL,
    GEMINI_GROUNDING_MODEL,
    GEMINI_PRIMARY_MODEL,
    RUNTIME_GEMINI_MODELS,
    normalize_gemini_chat_model,
    normalize_gemini_runtime_model,
    settings,
)
from app.errors import (
    ErrorCode,
    classify_key_error,
    is_error_message,
    is_key_related_error,
    is_retryable_error,
    tag_error,
    user_friendly_error,
)
from app.providers.base import is_freetheai_model, is_opencode_model
from app.providers.openrouter import _has_multimodal_content


def _setting(name: str, fallback: str) -> str:
    value = getattr(settings, name, None) if settings is not None else None
    return value if isinstance(value, str) and value.strip() else fallback


def _gemini_setting(name: str, fallback: str) -> str:
    return normalize_gemini_chat_model(_setting(name, fallback), fallback=fallback)


def _available_models() -> list[str]:
    value = getattr(settings, "AVAILABLE_MODELS", None) if settings is not None else None
    return value if isinstance(value, list) and value else DEFAULT_GEMINI_MODELS


# ── Opencode Go → Gemini cross-provider fallback map ──────────────────────────
# When ALL Opencode Go keys are exhausted or fail, silently retry on Gemini.
# Returns live mapping so hot-reloaded model names are correctly reflected.
def _get_opencode_gemini_fallback() -> dict[str, str]:
    """Build the Opencode → Gemini fallback map from current (live) settings.

    Covers all 14 Opencode Go models (opencode.ai/docs/go, 2026-05-01).
    Vision-capable models fall back to gemini-3.5-flash for image support.
    """
    return {
        # GLM family
        "opencode-go/glm-5": _gemini_setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
        "opencode-go/glm-5.1": _gemini_setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
        # Kimi family
        "opencode-go/kimi-k2.5": _gemini_setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
        "opencode-go/kimi-k2.6": _gemini_setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
        # MiMo family (V2 + V2.5)
        "opencode-go/mimo-v2-pro": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
        "opencode-go/mimo-v2-omni": GEMINI_PRIMARY_MODEL,  # vision-capable fallback
        "opencode-go/mimo-v2.5-pro": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
        "opencode-go/mimo-v2.5": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
        # MiniMax family
        "opencode-go/minimax-m2.5": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
        "opencode-go/minimax-m2.7": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
        # Qwen family
        "opencode-go/qwen3.5-plus": _gemini_setting("QNA_MODEL", GEMINI_ECONOMY_MODEL),
        "opencode-go/qwen3.6-plus": _gemini_setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
        # DeepSeek family
        "opencode-go/deepseek-v4-pro": _gemini_setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
        "opencode-go/deepseek-v4-flash": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
        # Legacy / routing alias
        "opencode-go/big-pickle": _gemini_setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
    }


def _dedupe_models(models: list[str | None], *, exclude: str | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for model in models:
        if not isinstance(model, str) or not model.strip():
            continue
        normalized = model.strip()
        if normalized == exclude or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _ordered_gemini_fallback_models(failed_model: str) -> list[str]:
    """Return configured Gemini fallbacks with 3.5 Flash → 3.1 Flash Lite first."""
    runtime_failed_model = normalize_gemini_runtime_model(failed_model)
    if runtime_failed_model == GEMINI_GROUNDING_MODEL:
        return [GEMINI_GROUNDING_FALLBACK_MODEL]
    if runtime_failed_model == GEMINI_GROUNDING_FALLBACK_MODEL:
        return []

    available = [
        model
        for model in (list(getattr(settings, "AVAILABLE_MODELS", []) or []) if settings is not None else [])
        if model in CURRENT_GEMINI_MODELS
    ]
    available_set = set(available)
    failed_model = normalize_gemini_chat_model(runtime_failed_model)
    preferred: list[str | None] = []
    if failed_model == GEMINI_PRIMARY_MODEL:
        preferred.append(GEMINI_ECONOMY_MODEL)
    else:
        preferred.extend([GEMINI_PRIMARY_MODEL, GEMINI_ECONOMY_MODEL])
    preferred.extend(
        [
            _setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
            _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
            _setting("QNA_MODEL", GEMINI_ECONOMY_MODEL),
            _setting("INLINE_MODEL", GEMINI_ECONOMY_MODEL),
        ]
    )
    ordered = [
        model
        for model in _dedupe_models(preferred, exclude=failed_model)
        if model in available_set and model in CURRENT_GEMINI_MODELS
    ]
    ordered.extend(model for model in available if model != failed_model and model not in ordered)
    return ordered


def _provider_label(model_name: str | None, use_openrouter: bool | None) -> str:
    if model_name and is_opencode_model(model_name):
        return "opencode"
    if model_name and is_freetheai_model(model_name):
        return "freetheai"
    if use_openrouter is True or (use_openrouter is None and isinstance(model_name, str) and "/" in model_name):
        return "openrouter"
    return "gemini"


def _key_prefix(api_key: str) -> str:
    return api_key[:8]


def _log_key_request(
    api_key: str,
    model_name: str | None,
    use_openrouter: bool | None,
    *,
    attempt: int | None = None,
    max_attempts: int | None = None,
) -> None:
    attempt_text = f" attempt={attempt}/{max_attempts}" if attempt is not None and max_attempts is not None else ""
    logging.info(
        "KEY_EVENT key_request key=%s… model=%s provider=%s%s",
        _key_prefix(api_key),
        model_name,
        _provider_label(model_name, use_openrouter),
        attempt_text,
    )


def _log_key_answered(
    api_key: str,
    model_name: str | None,
    use_openrouter: bool | None,
    token_count: int | None,
) -> None:
    logging.info(
        "KEY_EVENT key_answered key=%s… model=%s provider=%s tokens=%s",
        _key_prefix(api_key),
        model_name,
        _provider_label(model_name, use_openrouter),
        token_count if token_count is not None else "unknown",
    )


class ProviderRouter:
    """
    Routes AI requests to the right provider with key rotation and health scoring.

    Uses DB-backed KeyStatusManager for persistent per-model key health tracking.
    Keys are suspended with error-category-aware cooldowns and automatically
    recover after their cooldown expires (two-tier selection in SQL).
    """

    def __init__(self, rate_limit_per_minute: int = 20) -> None:
        # Use the consolidated RateLimiter from security.py (includes periodic cleanup)
        from app.security import RateLimiter

        self._rate_limiter = RateLimiter(max_requests=rate_limit_per_minute, window_seconds=60)

    async def get_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
        timeout: float | None = None,
        *,
        _is_fallback: bool = False,
    ) -> tuple[str, int | None]:
        """
        Get AI response with automatic key rotation and health-aware selection.

        Delegates to AgentRequestUseCase for key resolution, which uses
        two-tier SQL (active first, then cooldown-expired) to pick keys.
        On failure, classifies the error and suspends the key with appropriate
        cooldown. On success, promotes the key back to active.
        """
        from app.agent_use_cases import AgentRequestUseCase
        from app.repos.keys import get_key_status_manager

        # Per-user rate limiting (async — RateLimiter from security.py)
        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            return (
                tag_error(
                    ErrorCode.RATE_LIMIT,
                    "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
                ),
                None,
            )

        # Auto-detect multimodal content.
        # For Opencode vision models (mimo-v2-omni / mimo-v2-pro), allow multimodal
        # passthrough — OpencodeGoProvider supports image_url in messages.
        # For all other models, force Gemini path which has native vision.
        if use_openrouter is None and _has_multimodal_content(history) and not is_opencode_model(preferred_model):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True  # Track if ALL failures are permanent (model-level)
        had_transient: bool = False

        for attempt in range(max_key_retries):
            key_data, model_used, resolution = await use_case.resolve_ai_request(
                preferred_model,
                use_openrouter=use_openrouter,
                excluded_key_hashes=failed_keys,
            )

            if not key_data:
                if resolution == "all_exhausted":
                    # ── Cross-provider fallback: Opencode Go → Gemini ─────────
                    if is_opencode_model(preferred_model) and not _is_fallback:
                        gemini_fallback = _get_opencode_gemini_fallback().get(
                            preferred_model,
                            _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
                        )
                        logging.warning(
                            "Opencode keys exhausted for %s, falling back to Gemini %s",
                            preferred_model,
                            gemini_fallback,
                        )
                        return await self.get_response(
                            gemini_fallback,
                            history,
                            system_instruction=system_instruction,
                            user_id=user_id,
                            chat_id=chat_id,
                            use_openrouter=False,
                            max_key_retries=max_key_retries,
                            thinking_level=thinking_level,
                            timeout=timeout,
                            _is_fallback=True,
                        )
                    # ── Cross-provider fallback: FreeTheAI → Gemini ──────────
                    if is_freetheai_model(preferred_model) and not _is_fallback:
                        logging.warning(
                            "FreeTheAI keys exhausted for %s, falling back to Gemini %s",
                            preferred_model,
                            _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
                        )
                        gemini_fallback = _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL)
                        return await self.get_response(
                            gemini_fallback,
                            history,
                            system_instruction=system_instruction,
                            user_id=user_id,
                            chat_id=chat_id,
                            use_openrouter=False,
                            max_key_retries=max_key_retries,
                            thinking_level=thinking_level,
                            timeout=timeout,
                            _is_fallback=True,
                        )
                    is_or = (
                        use_openrouter
                        if use_openrouter is not None
                        else (
                            "/" in preferred_model
                            and not is_opencode_model(preferred_model)
                            and not is_freetheai_model(preferred_model)
                        )
                    )
                    provider_name = (
                        "Opencode Go"
                        if is_opencode_model(preferred_model)
                        else (
                            "FreeTheAI"
                            if is_freetheai_model(preferred_model)
                            else ("OpenRouter" if is_or else "Gemini")
                        )
                    )
                    return (
                        tag_error(
                            ErrorCode.KEYS_EXHAUSTED,
                            f"🚫 Все ключи {provider_name} недоступны или исчерпаны. Попробуйте позже.",
                        ),
                        None,
                    )
                if resolution == "no_keys":
                    return (
                        tag_error(
                            ErrorCode.NO_KEYS,
                            "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                        ),
                        None,
                    )
                if resolution == "decryption_failed":
                    return (
                        tag_error(
                            ErrorCode.DECRYPTION_FAILED,
                            "🔐 Ошибка расшифровки API-ключей. Обратитесь к администратору (возможно, изменился ADMIN_SECRET).",
                        ),
                        None,
                    )
                return (
                    tag_error(
                        ErrorCode.KEYS_EXHAUSTED,
                        "🚫 Не удалось получить доступный ключ API. Попробуйте позже.",
                    ),
                    None,
                )

            # Execute the request
            assert model_used is not None  # guaranteed by _resolve_ai_request
            _log_key_request(
                key_data["api_key"],
                model_used,
                use_openrouter,
                attempt=attempt + 1,
                max_attempts=max_key_retries,
            )
            try:
                response_text, token_count = await use_case.get_ai_response(
                    key_data["api_key"],
                    history,
                    model_used,
                    system_instruction,
                    user_id,
                    chat_id,
                    use_openrouter,
                    thinking_level=thinking_level,
                    timeout=timeout,
                    provider_max_retries=1,
                )
            except Exception as exc:
                failed_keys.add(key_data["key_hash"])
                response_text = user_friendly_error(exc)
                error_category = classify_key_error(response_text)
                if error_category == "transient":
                    had_transient = True
                if error_category != "permanent":
                    all_permanent = False
                try:
                    if not is_opencode_model(model_used) and not is_freetheai_model(model_used):
                        await status_mgr.suspend_key(
                            key_data["key_hash"],
                            model_used,
                            error_category,
                            str(exc)[:200] or type(exc).__name__,
                        )
                except Exception as suspend_exc:
                    logging.warning("Non-critical: failed to suspend key after exception: %s", suspend_exc)
                logging.warning(
                    "Key %s… raised %s (category=%s, attempt %d/%d). Retrying with another key.",
                    key_data["key_hash"][:8],
                    type(exc).__name__,
                    error_category,
                    attempt + 1,
                    max_key_retries,
                )
                continue

            # Track health based on response
            if response_text and is_error_message(response_text) and (
                is_key_related_error(response_text) or is_retryable_error(response_text)
            ):
                failed_keys.add(key_data["key_hash"])
                error_category = classify_key_error(response_text)

                if error_category == "transient":
                    had_transient = True
                if error_category != "permanent":
                    all_permanent = False

                # Suspend the key for ALL error categories including transient (503/OVERLOADED).
                # Previously the "!= transient" guard skipped suspension, leaving keys marked
                # healthy in DB. The next card then resolved the same overloaded keys again.
                # _PENALTY_DURATIONS["transient"] = 15s already existed but was never reached.
                # get_fresh_available_key() SQL filters suspended keys (suspended_until < NOW())
                # so recovery is automatic after the cooldown expires.
                try:
                    if not is_opencode_model(model_used) and not is_freetheai_model(model_used):  # type: ignore[arg-type]
                        await status_mgr.suspend_key(
                            key_data["key_hash"],
                            model_used,  # type: ignore[arg-type]  # asserted above
                            error_category,
                            response_text[:200],
                        )
                except Exception as e:
                    logging.warning(
                        "Non-critical: failed to suspend key: %s",
                        e,
                    )

                logging.warning(
                    "Key %s… failed (category=%s, attempt %d/%d). Error: %s",
                    key_data["key_hash"][:8],
                    error_category,
                    attempt + 1,
                    max_key_retries,
                    response_text[:100],
                )
                continue

            # Success — update health and increment usage
            if response_text and not is_error_message(response_text):
                try:
                    # Opencode/FTA keys are in-memory only — skip DB key_model_status writes
                    # (the trigger check_key_hash_exists() only knows api_keys/openrouter_api_keys)
                    if not is_opencode_model(model_used) and not is_freetheai_model(model_used):  # type: ignore[arg-type]
                        await status_mgr.record_success(
                            key_data["key_hash"],
                            model_used,  # type: ignore[arg-type]  # asserted above
                        )
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)

                try:
                    await use_case.increment_key_usage(key_data["key_hash"], model_used, use_openrouter)  # type: ignore[arg-type]  # asserted above
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)

                _log_key_answered(key_data["api_key"], model_used, use_openrouter, token_count)

            return response_text, token_count

        # ── Model-level fallback ─────────────────────────────────────────
        if not _is_fallback and had_transient:
            fallback_model = self._pick_transient_fallback_model(preferred_model, use_openrouter)
            if fallback_model:
                logging.info(
                    "Cascade fallback: %s → %s (all keys returned transient errors)",
                    preferred_model,
                    fallback_model,
                )
                return await self.get_response(
                    fallback_model,
                    history,
                    system_instruction=system_instruction,
                    user_id=user_id,
                    chat_id=chat_id,
                    use_openrouter=use_openrouter,
                    max_key_retries=2,
                    thinking_level=thinking_level,
                    timeout=timeout,
                    _is_fallback=True,
                )

        # All keys failed for the preferred model. If every failure was
        # "permanent" (API_KEY_INVALID — Google rejects the key for this
        # specific model), try alternative models before giving up.
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model,
                history,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                use_case,
                status_mgr,
            )
            if fallback_result is not None:
                return fallback_result

        is_or = (
            use_openrouter
            if use_openrouter is not None
            else (
                "/" in preferred_model
                and not is_opencode_model(preferred_model)
                and not is_freetheai_model(preferred_model)
            )
        )
        provider_name = (
            "Opencode Go"
            if is_opencode_model(preferred_model)
            else (
                "FreeTheAI"
                if is_freetheai_model(preferred_model)
                else ("OpenRouter" if is_or else "Gemini")
            )
        )
        return (
            tag_error(
                ErrorCode.KEYS_EXHAUSTED,
                f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
            ),
            None,
        )

    async def stream_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
        enable_web_search: bool = False,
        force_grounding: bool = False,
        *,
        _is_fallback: bool = False,
    ):

        """
        Stream AI response with Race Requests and automatic key rotation.

        Race Requests: on each attempt, resolves up to 2 keys and fires them
        in parallel. The first key to yield a chunk wins; the loser is cancelled.
        Zero artificial delays between retries for maximum speed.

        After exhausting all retries for transient errors (503), cascades to a
        lighter fallback model before returning an error to the user.

        Yields chunks of text.
        """

        from app.agent_use_cases import AgentRequestUseCase
        from app.providers.base import get_provider_for_model
        from app.repos.keys import get_key_status_manager

        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            yield tag_error(
                ErrorCode.RATE_LIMIT,
                "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
            )
            return

        if use_openrouter is None and _has_multimodal_content(history) and not is_opencode_model(preferred_model):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True
        had_transient: bool = False  # Track if any 503/transient errors occurred

        for _attempt in range(max_key_retries):
            # ── Race Requests: resolve up to 2 keys in parallel ──────────
            keys_to_race: list[dict] = []
            resolved_model: str | None = None
            resolution_status: str | None = None

            for _race_idx in range(2):
                key_data, model_used, _resolution = await use_case.resolve_ai_request(
                    preferred_model,
                    use_openrouter=use_openrouter,
                    excluded_key_hashes=failed_keys | {k["key_hash"] for k in keys_to_race},
                )
                resolution_status = _resolution
                if key_data and model_used:
                    keys_to_race.append(key_data)
                    resolved_model = model_used
                else:
                    break  # No more keys available

            # ── Inject Vertex AI Express Slot ─────────────────────────
            # Only for supported models (currently gemini-3.1-flash-lite)
            # and only if we have at least one valid Gemini key to race alongside it.
            _VERTEX_KH = "__vertex_ai__"
            if keys_to_race and resolved_model and "gemini-3.1-flash-lite" in resolved_model:
                from app.providers.gemini import is_vertex_client_available

                if is_vertex_client_available() and _VERTEX_KH not in failed_keys:
                    keys_to_race.append({"api_key": "vertex", "key_hash": _VERTEX_KH})

            if not keys_to_race or not resolved_model:
                if resolution_status == "decryption_failed":
                    yield tag_error(
                        ErrorCode.DECRYPTION_FAILED,
                        "🔐 Ошибка расшифровки API-ключей. Обратитесь к администратору (возможно, изменился ADMIN_SECRET).",
                    )
                    return
                # ── Cross-provider fallback: Opencode Go → Gemini (streaming) ─────
                if is_opencode_model(preferred_model) and not _is_fallback:
                    gemini_fallback = _get_opencode_gemini_fallback().get(
                        preferred_model,
                        _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
                    )
                    logging.warning(
                        "Opencode stream keys unavailable for %s, cascading to Gemini %s",
                        preferred_model,
                        gemini_fallback,
                    )
                    async for chunk in self.stream_response(
                        preferred_model=gemini_fallback,
                        history=history,
                        system_instruction=system_instruction,
                        user_id=user_id,
                        chat_id=chat_id,
                        use_openrouter=False,
                        max_key_retries=max_key_retries,
                        thinking_level=thinking_level,
                        enable_web_search=enable_web_search,
                        force_grounding=force_grounding,
                        _is_fallback=True,

                    ):
                        yield chunk
                    return
                is_or = (
                    use_openrouter
                    if use_openrouter is not None
                    else (
                        "/" in preferred_model
                        and not is_opencode_model(preferred_model)
                        and not is_freetheai_model(preferred_model)
                    )
                )
                provider_name = (
                    "Opencode Go"
                    if is_opencode_model(preferred_model)
                    else (
                        "FreeTheAI"
                        if is_freetheai_model(preferred_model)
                        else ("OpenRouter" if is_or else "Gemini")
                    )
                )
                yield tag_error(
                    ErrorCode.KEYS_EXHAUSTED,
                    f"🚫 Все ключи {provider_name} недоступны.",
                )
                return

            model_used = resolved_model

            if len(keys_to_race) == 1:
                # Single key available — use direct streaming (no race overhead)
                key_data = keys_to_race[0]
                kh = key_data["key_hash"][:8]
                raw_key = key_data.get("api_key", "")
                key_suffix = raw_key[-4:] if len(raw_key) >= 4 else "????"
                logging.info(
                    "Streaming: model=%s key=%s…(…%s) attempt=%d/%d",
                    model_used,
                    kh,
                    key_suffix,
                    _attempt + 1,
                    max_key_retries,
                )

                provider = get_provider_for_model(model_used, key_data["api_key"])
                stream_started = False
                try:
                    async for chunk in provider.stream_response(  # type: ignore[attr-defined]
                        history=history,
                        model_name=model_used,
                        system_instruction=system_instruction,
                        thinking_level=thinking_level,
                        enable_web_search=enable_web_search,
                        force_grounding=force_grounding,
                    ):

                        # Guard: provider may yield a tagged error string instead of
                        # raising an exception (e.g. OpenRouter 429 → RATE_LIMIT tag).
                        # Treat that as a stream-level failure so key rotation kicks in.
                        if not stream_started and is_error_message(chunk):
                            raise RuntimeError(f"Provider yielded error tag before streaming: {chunk[:200]}")

                        if not stream_started:
                            stream_started = True
                            try:
                                # Opencode/FTA keys are in-memory only — skip DB writes
                                if not is_opencode_model(model_used) and not is_freetheai_model(model_used):
                                    await status_mgr.record_success(key_data["key_hash"], model_used)
                                await use_case.increment_key_usage(key_data["key_hash"], model_used, use_openrouter)
                            except Exception as e:
                                logging.debug("Non-critical stats update failed: %s", e)
                        yield chunk

                    if stream_started:
                        return

                except Exception as e:
                    if stream_started:
                        logging.error("Stream failed mid-flight, escalating to streaming layer: %s", e)
                        raise

                    error_msg = str(e)
                    # Use the error tag (if present) to determine the correct penalty
                    # category rather than always hard-coding "transient".
                    # e.g. RATE_LIMIT → "rate_limit" (15 s cooldown),
                    #      QUOTA_EXCEEDED → "quota" (until midnight PT).
                    inner_tag = error_msg[error_msg.find("\u200b[") :] if "\u200b[" in error_msg else error_msg
                    error_category = classify_key_error(inner_tag)
                    if error_category == "transient":
                        had_transient = True
                    if error_category != "permanent":
                        all_permanent = False

                    failed_keys.add(key_data["key_hash"])
                    logging.warning(
                        "Single-key stream failed (category=%s) for key=%s… model=%s: %s",
                        error_category,
                        key_data["key_hash"][:8],
                        model_used,
                        error_msg[:120],
                    )
                    try:
                        # Opencode/FTA keys are in-memory only — skip DB suspension writes
                        if not is_opencode_model(model_used) and not is_freetheai_model(model_used):
                            await status_mgr.suspend_key(
                                key_data["key_hash"],
                                model_used,
                                error_category,
                                error_msg[:200],
                            )
                    except Exception as db_e:
                        logging.warning("Failed to suspend key: %s", db_e)
                    continue  # Next retry attempt — no sleep!

            else:
                # ── Race: 2 keys in parallel ─────────────────────────────
                #
                # Strategy: fire both streams as tasks. The first to yield a chunk
                # wins — we cancel the loser and forward the winner's chunks.
                # If both fail before yielding, mark both as failed and retry.
                # Sentinel-based completion: each racer puts a _StreamEndSignal after
                # all chunks, so the consumer never relies on task.done() which
                # has a TOCTOU race (task finishes while last chunk is still in queue).
                # _StreamEndSignal also carries finish_reason from the task's own ContextVar
                # (asyncio.create_task copies ContextVars so mutations inside the task
                # are invisible to the parent).

                class _StreamEndSignal:
                    """Sentinel put by the producer after all chunks.

                    Carries finish_reason extracted from within the task's own context,
                    since asyncio.create_task copies ContextVars and mutations inside
                    the task are invisible to the parent context.
                    """

                    __slots__ = ("finish_reason",)

                    def __init__(self, finish_reason: str | None) -> None:
                        self.finish_reason = finish_reason

                winner_queue: asyncio.Queue[tuple[int, str | object | None, Exception | None]] = asyncio.Queue()

                async def _race_stream(
                    idx: int,
                    kd: dict,
                    mod: str = str(model_used),
                    q: asyncio.Queue = winner_queue,
                ) -> None:
                    """Race participant: stream from one key, push chunks + sentinel to queue."""
                    first_chunk_seen = False
                    try:
                        prov = get_provider_for_model(mod, kd["api_key"])
                        async for chunk in prov.stream_response(  # type: ignore[attr-defined]
                            history=history,
                            model_name=mod,
                            system_instruction=system_instruction,
                            thinking_level=thinking_level,
                            enable_web_search=enable_web_search,
                            force_grounding=force_grounding,
                        ):

                            # Guard: provider may yield a tagged error string instead of
                            # raising an exception — treat it as a race failure so the
                            # partner key can win the race and serve the user.
                            if not first_chunk_seen and is_error_message(chunk):
                                raise RuntimeError(f"Provider yielded error tag before streaming: {chunk[:200]}")
                            first_chunk_seen = True
                            await q.put((idx, chunk, None))
                    except asyncio.CancelledError:
                        # Loser was cancelled — put sentinel so consumer doesn't hang
                        await q.put((idx, _StreamEndSignal(None), None))
                        return
                    except Exception as exc_:
                        await q.put((idx, None, exc_))
                        return
                    # Normal completion: read finish_reason from this task's own ContextVar
                    # and carry it in the sentinel so the parent context can apply it.
                    from app.streaming import _last_finish_reason as _fr_var

                    own_fr = _fr_var.get()
                    await q.put((idx, _StreamEndSignal(own_fr), None))

                # Log race participants
                khs = [kd["key_hash"][:8] for kd in keys_to_race]
                logging.info(
                    "Race Requests: model=%s keys=%s attempt=%d/%d",
                    model_used,
                    khs,
                    _attempt + 1,
                    max_key_retries,
                )

                tasks = [asyncio.create_task(_race_stream(i, kd)) for i, kd in enumerate(keys_to_race)]

                winner_idx: int | None = None
                race_errors: dict[int, Exception] = {}

                # Wait for the first signal from either racer
                while winner_idx is None and len(race_errors) < len(keys_to_race):
                    try:
                        idx, chunk, exc = await asyncio.wait_for(winner_queue.get(), timeout=30.0)
                    except TimeoutError:
                        # Both racers hung — cancel both and retry
                        for t in tasks:
                            t.cancel()
                        for kd in keys_to_race:
                            failed_keys.add(kd["key_hash"])
                        had_transient = True
                        all_permanent = False
                        break
                    if exc is not None:
                        race_errors[idx] = exc
                        continue
                    if isinstance(chunk, _StreamEndSignal):
                        # Racer finished without yielding any real chunk — treat as error
                        race_errors[idx] = RuntimeError("stream ended without chunks")
                        continue
                    # We have a winner!
                    winner_idx = idx

                if winner_idx is None:
                    # Both failed (or timed out) — mark keys as failed, no sleep, retry
                    for t in tasks:
                        t.cancel()
                    for i, kd in enumerate(keys_to_race):
                        failed_keys.add(kd["key_hash"])
                        # Safe default: treat unknown errors as transient
                        err_category = "transient"
                        try:
                            raw_err = str(race_errors.get(i, "race timeout"))
                            # Derive penalty category from the error tag embedded in the
                            # message (e.g. RATE_LIMIT, QUOTA_EXCEEDED) rather than always
                            # defaulting to "transient", so cooldown durations are accurate.
                            inner_tag = raw_err[raw_err.find("\u200b[") :] if "\u200b[" in raw_err else raw_err
                            err_category = classify_key_error(inner_tag)
                            logging.warning(
                                "Race key=%s… failed (category=%s): %s",
                                kd["key_hash"][:8],
                                err_category,
                                raw_err[:120],
                            )
                            if kd["key_hash"] == _VERTEX_KH:
                                from app.providers.gemini import report_vertex_error

                                report_vertex_error(RuntimeError(raw_err))
                            elif not is_opencode_model(model_used) and not is_freetheai_model(model_used):
                                await status_mgr.suspend_key(
                                    kd["key_hash"], model_used, err_category, raw_err[:200]
                                )
                        except Exception:
                            pass
                        # Update outer flags regardless of whether suspend_key succeeded
                        if err_category == "transient":
                            had_transient = True
                        if err_category != "permanent":
                            all_permanent = False
                    continue  # Next retry — zero delay!

                # Cancel the losers
                for i, t in enumerate(tasks):
                    if i != winner_idx:
                        t.cancel()
                winner_key = keys_to_race[winner_idx]

                # Record success for the winner.
                # Skip DB writes for the Vertex AI pseudo-key — it is a sentinel
                # (not a row in api_keys) and the FK on key_usage would fire.
                # Same guard as inline.py:1230.
                if winner_key["key_hash"] != _VERTEX_KH:
                    try:
                        if not is_opencode_model(model_used) and not is_freetheai_model(model_used):
                            await status_mgr.record_success(winner_key["key_hash"], model_used)
                        await use_case.increment_key_usage(winner_key["key_hash"], model_used, use_openrouter)
                    except Exception as e:
                        logging.debug("Non-critical stats update failed: %s", e)

                loser_khs = [k["key_hash"][:8] for i, k in enumerate(keys_to_race) if i != winner_idx]
                logging.info(
                    "Race winner: key=%s… (losers %s cancelled)",
                    winner_key["key_hash"][:8],
                    loser_khs,
                )

                # Yield the first winning chunk
                assert chunk is not None
                yield chunk

                # Forward remaining chunks from winner — break only on sentinel
                try:
                    while True:
                        try:
                            idx, chunk, exc = await asyncio.wait_for(winner_queue.get(), timeout=120.0)
                        except TimeoutError:
                            logging.warning("Race drain timed out after 120s — forcing exit")
                            break
                        if idx != winner_idx:
                            continue  # Stale chunk from loser
                        if isinstance(chunk, _StreamEndSignal):
                            # Winner stream completed — apply finish_reason to parent context
                            if chunk.finish_reason:
                                from app.streaming import set_last_finish_reason

                                set_last_finish_reason(chunk.finish_reason)
                            break  # All chunks delivered
                        if exc is not None:
                            raise exc
                        if chunk is not None:
                            yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.error("Race winner stream failed mid-flight: %s", e)
                    raise
                finally:
                    # Suppress unhandled-task-exception warning from loser
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    try:
                        await asyncio.wait(tasks, timeout=0.5)
                    except Exception:
                        pass
                return  # Race completed successfully

        # ── Exhausted retries — try model fallback ───────────────────────
        # For transient errors (503), cascade to lighter model before giving up.
        # This is the key UX improvement: user gets an answer from lite model
        # instead of a cold error message.
        if not _is_fallback and had_transient:
            # Determine fallback model — prefer lite variant of same family
            fallback_model = self._pick_transient_fallback_model(preferred_model, use_openrouter)
            if fallback_model:
                logging.info(
                    "Cascade fallback: %s → %s (all keys returned transient errors)",
                    preferred_model,
                    fallback_model,
                )
                async for chunk in self.stream_response(
                    preferred_model=fallback_model,
                    history=history,
                    system_instruction=system_instruction,
                    user_id=user_id,
                    chat_id=chat_id,
                    use_openrouter=use_openrouter,
                    max_key_retries=2,  # Fewer retries for fallback
                    thinking_level=thinking_level,
                    enable_web_search=enable_web_search,
                    force_grounding=force_grounding,
                    _is_fallback=True,

                ):
                    yield chunk
                return

        # Permanent-error model fallback (existing behavior)
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model,
                history,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                use_case,
                status_mgr,
            )
            if fallback_result is not None:
                yield fallback_result[0]
                return

        # ── Deferred Queue (Plan §5): last resort before hard error ──────
        # If user_id and chat_id are available, enqueue for background retry
        # instead of showing a cold error.
        if user_id and chat_id and had_transient:
            try:
                from app.deferred_response import enqueue_deferred_generation

                task_id = await enqueue_deferred_generation(
                    user_id=user_id,
                    chat_id=chat_id,
                    history=history,
                    model_name=preferred_model,
                    system_instruction=system_instruction,
                )
                if task_id:
                    yield tag_error(
                        ErrorCode.KEYS_EXHAUSTED,
                        "⏳ Серверы AI временно перегружены. Я отправлю ответ, как только они освободятся.",
                    )
                    return
            except Exception as e:
                logging.warning("Deferred queue fallback failed: %s", e)

        is_or = (
            use_openrouter
            if use_openrouter is not None
            else (
                "/" in preferred_model
                and not is_opencode_model(preferred_model)
                and not is_freetheai_model(preferred_model)
            )
        )
        provider_name = (
            "Opencode Go"
            if is_opencode_model(preferred_model)
            else (
                "FreeTheAI"
                if is_freetheai_model(preferred_model)
                else ("OpenRouter" if is_or else "Gemini")
            )
        )
        yield tag_error(
            ErrorCode.KEYS_EXHAUSTED,
            f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
        )
        return

    def _pick_transient_fallback_model(
        self,
        failed_model: str,
        use_openrouter: bool | None,
    ) -> str | None:
        """Pick a lighter fallback model for transient (503) errors.

        Maps heavy models to their lite counterparts. Returns None if
        the failed model is already the lightest available.
        """
        # Gemini cascade: current primary → current economy; grounding stays
        # on the Gemini 2.5 family because free-tier AI Studio keys do not
        # support Gemini 3+ Google Search grounding.
        _GEMINI_CASCADE = {
            GEMINI_PRIMARY_MODEL: GEMINI_ECONOMY_MODEL,
            GEMINI_GROUNDING_MODEL: GEMINI_GROUNDING_FALLBACK_MODEL,
        }

        # Opencode Go: cascade to Gemini via the cross-provider fallback map
        if is_opencode_model(failed_model):
            gemini_fallback = _get_opencode_gemini_fallback().get(
                failed_model,
                _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
            )
            if gemini_fallback in _available_models():
                return gemini_fallback
            return None

        is_or = (
            use_openrouter
            if use_openrouter is not None
            else ("/" in failed_model and not is_opencode_model(failed_model))
        )
        if is_or:
            return None  # OpenRouter handles its own fallbacks

        gemini_failed_model = normalize_gemini_runtime_model(failed_model)
        fallback = _GEMINI_CASCADE.get(gemini_failed_model)
        if fallback:
            if fallback in (GEMINI_GROUNDING_FALLBACK_MODEL,):
                return fallback
            # Verify the fallback model is actually in our available models list
            available = [model for model in _available_models() if model in RUNTIME_GEMINI_MODELS]
            if fallback in available:
                return fallback

        return None

    async def _try_model_fallback(
        self,
        failed_model: str,
        history: list,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        use_openrouter: bool | None,
        use_case,
        status_mgr,
    ) -> tuple[str, int | None] | None:
        """Try fallback models when all keys fail with permanent errors for one model.

        Returns (response_text, token_count) on success, or None if no fallback works.
        """
        from app.errors import is_error_message

        _is_or = (
            use_openrouter
            if use_openrouter is not None
            else ("/" in failed_model and not is_opencode_model(failed_model))
        )

        # Never use OpenRouter for fallback according to user request.
        # Fallback always uses the reliable production models (Gemini)
        fallback_models = _ordered_gemini_fallback_models(failed_model)

        for fallback_model in fallback_models:
            if fallback_model == failed_model:
                continue

            key_data, model_used, _ = await use_case.resolve_ai_request(
                fallback_model,
                use_openrouter=use_openrouter,
            )
            if not key_data:
                continue

            logging.info(
                "Model fallback: trying %s instead of %s (all keys rejected by API for original model)",
                fallback_model,
                failed_model,
            )

            _log_key_request(key_data["api_key"], model_used, use_openrouter)
            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                provider_max_retries=1,
            )

            if response_text and not is_error_message(response_text):
                logging.info(
                    "Model fallback succeeded: %s → %s",
                    failed_model,
                    model_used,
                )
                try:
                    await status_mgr.record_success(key_data["key_hash"], model_used)
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)
                try:
                    await use_case.increment_key_usage(
                        key_data["key_hash"],
                        model_used,
                        use_openrouter,
                    )
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)
                _log_key_answered(key_data["api_key"], model_used, use_openrouter, token_count)
                return response_text, token_count

            logging.warning(
                "Model fallback %s also failed: %s",
                fallback_model,
                (response_text or "")[:100],
            )

        return None

    async def get_key_stats(self) -> list[dict[str, Any]]:
        """Return health stats for all tracked keys (for diagnostics)."""
        from app.repos.keys import get_key_status_manager

        return await get_key_status_manager().get_all_statuses()


# Module-level singleton
_provider_router: ProviderRouter | None = None


def get_provider_router() -> ProviderRouter:
    """Get the singleton ProviderRouter instance."""
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router


async def get_ai_response(
    api_key: str,
    history: list[dict[str, Any]],
    model_name: str,
    system_instruction: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    max_retries: int = 3,
) -> tuple[str, int | None]:
    """
    Unified entry point for AI responses.

    Automatically selects the appropriate provider based on model name.
    Returns tuple (response_text, token_count) for backwards compatibility.

    Args:
        api_key: API key for the provider
        history: Message history
        model_name: Model identifier (Gemini or OpenRouter format)
        system_instruction: Optional system prompt
        user_id: User ID for logging
        chat_id: Chat ID for logging
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (response_text, token_count)
    """
    from app.providers.base import get_provider_for_model

    provider = get_provider_for_model(model_name, api_key)

    response = await provider.get_response(
        history=history,
        model_name=model_name,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
        max_retries=max_retries,
    )

    return response.text, response.token_count if response.success else None
