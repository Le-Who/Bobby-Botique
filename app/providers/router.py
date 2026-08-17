"""
ProviderRouter — routes AI requests with key rotation and health scoring.

Also provides the module-level convenience functions:
- get_provider_router() → singleton
- get_ai_response()     → backward-compat wrapper
"""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from app.config import (
    CURRENT_GEMINI_MODELS,
    DEFAULT_GEMINI_MODELS,
    GEMINI_ECONOMY_MODEL,
    GEMINI_GROUNDING_FALLBACK_MODEL,
    GEMINI_GROUNDING_MODEL,
    GEMINI_PRIMARY_MODEL,
    is_gemini_chat_model_id,
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
from app.providers.stream_types import (
    FailurePhase,
    GenerationEvent,
    GenerationRequest,
    KeyDisposition,
    ProviderStreamProtocolError,
    RetryDisposition,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    Workload,
    is_terminal_event,
)


def _setting(name: str, fallback: str) -> str:
    value = getattr(settings, name, None) if settings is not None else None
    return value if isinstance(value, str) and value.strip() else fallback


def _gemini_setting(name: str, fallback: str) -> str:
    return normalize_gemini_chat_model(_setting(name, fallback), fallback=fallback)


def _available_models() -> list[str]:
    value = getattr(settings, "AVAILABLE_MODELS", None) if settings is not None else None
    return value if isinstance(value, list) else DEFAULT_GEMINI_MODELS


def _runtime_gemini_models() -> list[str]:
    """Return configured and internal Gemini runtime candidates without static eligibility checks."""
    configured = _available_models()
    role_models = [
        getattr(settings, name, None)
        for name in ("RESEARCH_MODEL", "DEFAULT_MODEL", "QNA_MODEL", "INLINE_MODEL")
    ]
    normalized = [
        normalize_gemini_runtime_model(model, fallback="")
        for model in _dedupe_models([*configured, *role_models])
    ]
    return [model for model in _dedupe_models(normalized) if is_gemini_chat_model_id(model)]


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


def _dedupe_models(models: Iterable[str | None], *, exclude: str | None = None) -> list[str]:
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
    """Return configured Gemini fallbacks following the 3.6-flash -> 3.5-flash -> 3.5-flash-lite -> 3.1-flash-lite chain."""
    runtime_failed_model = normalize_gemini_runtime_model(failed_model)
    if runtime_failed_model == GEMINI_GROUNDING_MODEL:
        return [GEMINI_GROUNDING_FALLBACK_MODEL]
    if runtime_failed_model == GEMINI_GROUNDING_FALLBACK_MODEL:
        return []

    failed_model_norm = normalize_gemini_chat_model(runtime_failed_model, fallback=failed_model)

    chain = list(CURRENT_GEMINI_MODELS)

    preferred: list[str] = []
    if failed_model_norm in chain:
        idx = chain.index(failed_model_norm)
        preferred = chain[idx + 1 :]
    else:
        preferred = [m for m in chain if m != failed_model_norm]

    preferred.extend(
        [
            _setting("RESEARCH_MODEL", GEMINI_PRIMARY_MODEL),
            _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
            _setting("QNA_MODEL", GEMINI_ECONOMY_MODEL),
            _setting("INLINE_MODEL", GEMINI_ECONOMY_MODEL),
        ]
    )
    preferred.extend(_available_models())
    normalized = [normalize_gemini_runtime_model(model, fallback="") for model in preferred]
    return [
        model
        for model in _dedupe_models(normalized, exclude=failed_model_norm)
        if is_gemini_chat_model_id(model)
    ]


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

    async def stream(self, request: GenerationRequest):
        """Route a typed generation stream and emit exactly one terminal event."""
        from app.agent_use_cases import AgentRequestUseCase
        from app.providers.base import get_provider_for_model
        from app.providers.request_factory import deferred_history_from_request
        from app.repos.keys import get_key_status_manager

        scope = request.scope
        if scope.user_id and not await self._rate_limiter.check_rate_limit(scope.user_id):
            yield StreamFailed(
                code=ErrorCode.RATE_LIMIT,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.RETRY_LATER,
                key=KeyDisposition.UNCHANGED,
                diagnostic="Per-user provider rate limit rejected request",
            )
            return

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        model_queue = list(request.models)
        attempted_models: set[str] = set()
        overall_had_transient = False
        last_failure: StreamFailed | None = None
        _VERTEX_KH = "__vertex_ai__"

        def _failure_category(failure: StreamFailed) -> str:
            return {
                KeyDisposition.INVALID: "permanent",
                KeyDisposition.EXHAUSTED: "quota",
                KeyDisposition.RATE_LIMITED: "rate_limit",
                KeyDisposition.TRANSIENT_FAILURE: "transient",
                KeyDisposition.UNCHANGED: "transient",
            }[failure.key]

        async def _record_success(key_data: dict, model_name: str) -> None:
            key_hash = key_data["key_hash"]
            try:
                if (
                    key_hash != _VERTEX_KH
                    and not is_opencode_model(model_name)
                    and not is_freetheai_model(model_name)
                ):
                    await status_mgr.record_success(key_hash, model_name)
                if key_hash != _VERTEX_KH:
                    await use_case.increment_key_usage(key_hash, model_name, None)
            except Exception as exc:
                logging.debug("Non-critical typed-stream stats update failed: %s", exc)

        async def _record_failure(
            key_data: dict,
            model_name: str,
            failure: StreamFailed,
        ) -> str:
            category = _failure_category(failure)
            key_hash = key_data["key_hash"]
            try:
                if key_hash == _VERTEX_KH:
                    from app.providers.gemini import report_vertex_error

                    report_vertex_error(RuntimeError(failure.diagnostic))
                elif not is_opencode_model(model_name) and not is_freetheai_model(model_name):
                    await status_mgr.suspend_key(
                        key_hash,
                        model_name,
                        category,
                        failure.diagnostic[:200],
                    )
            except Exception as exc:
                logging.warning("Failed to apply typed-stream key disposition: %s", exc)
            return category

        while model_queue:
            preferred_model = model_queue.pop(0)
            if preferred_model in attempted_models:
                continue
            attempted_models.add(preferred_model)

            failed_keys: set[str] = set()
            model_had_transient = False
            model_all_permanent = True
            resolution_status: str | None = None

            for attempt in range(request.key_attempt_rounds):
                keys_to_race: list[dict] = []
                resolved_model: str | None = None

                for _race_idx in range(2):
                    key_data, model_used, resolution_status = await use_case.resolve_ai_request(
                        preferred_model,
                        use_openrouter=None,
                        excluded_key_hashes=failed_keys
                        | {key["key_hash"] for key in keys_to_race},
                    )
                    if not key_data or not model_used:
                        break
                    keys_to_race.append(key_data)
                    resolved_model = model_used

                if (
                    keys_to_race
                    and resolved_model
                    and "gemini-3.1-flash-lite" in resolved_model
                ):
                    from app.providers.gemini import is_vertex_client_available

                    if is_vertex_client_available() and _VERTEX_KH not in failed_keys:
                        keys_to_race.append(
                            {"api_key": "vertex", "key_hash": _VERTEX_KH}
                        )

                if not keys_to_race or not resolved_model:
                    if resolution_status == "decryption_failed":
                        last_failure = StreamFailed(
                            code=ErrorCode.DECRYPTION_FAILED,
                            phase=FailurePhase.BEFORE_TEXT,
                            retry=RetryDisposition.DO_NOT_RETRY,
                            key=KeyDisposition.UNCHANGED,
                            diagnostic="Provider key decryption failed during routing",
                        )
                    elif last_failure is None:
                        last_failure = StreamFailed(
                            code=ErrorCode.KEYS_EXHAUSTED,
                            phase=FailurePhase.BEFORE_TEXT,
                            retry=RetryDisposition.RETRY_LATER,
                            key=KeyDisposition.EXHAUSTED,
                            diagnostic=f"No usable keys resolved for {preferred_model}",
                        )
                    break

                model_used = resolved_model
                logging.info(
                    "Typed stream route: model=%s keys=%s attempt=%d/%d",
                    model_used,
                    [key["key_hash"][:8] for key in keys_to_race],
                    attempt + 1,
                    request.key_attempt_rounds,
                )

                if len(keys_to_race) == 1:
                    key_data = keys_to_race[0]
                    provider = get_provider_for_model(model_used, key_data["api_key"])
                    saw_text = False
                    terminal_seen = False
                    failure: StreamFailed | None = None
                    terminal_result: GenerationEvent | None = None

                    async for event in provider.stream(request, model_name=model_used):
                        if terminal_seen:
                            raise ProviderStreamProtocolError(
                                f"{provider.provider_name} emitted an event after terminal"
                            )
                        if isinstance(event, TextDelta):
                            if not saw_text:
                                saw_text = True
                                await _record_success(key_data, model_used)
                            yield event
                            continue
                        if not is_terminal_event(event):
                            raise ProviderStreamProtocolError(
                                f"Unsupported provider event: {type(event).__name__}"
                            )
                        terminal_seen = True
                        if isinstance(event, StreamCompleted):
                            if not saw_text:
                                failure = StreamFailed(
                                    code=ErrorCode.EMPTY_RESPONSE,
                                    phase=FailurePhase.BEFORE_TEXT,
                                    retry=RetryDisposition.TRY_NEXT_KEY,
                                    key=KeyDisposition.TRANSIENT_FAILURE,
                                    diagnostic="Provider completed before emitting visible text",
                                    route=event.route,
                                )
                            else:
                                terminal_result = event
                        elif isinstance(event, StreamDeferred):
                            if saw_text:
                                raise ProviderStreamProtocolError(
                                    "Provider deferred after emitting visible text"
                                )
                            terminal_result = event
                        elif isinstance(event, StreamFailed):
                            if saw_text:
                                if event.phase is not FailurePhase.AFTER_TEXT:
                                    raise ProviderStreamProtocolError(
                                        "Provider reported BEFORE_TEXT after text"
                                    )
                                terminal_result = event
                            failure = event

                    if not terminal_seen:
                        raise ProviderStreamProtocolError(
                            f"{getattr(provider, 'provider_name', type(provider).__name__)} ended without terminal event"
                        )
                    if terminal_result is not None:
                        yield terminal_result
                        return
                    assert failure is not None
                    failed_keys.add(key_data["key_hash"])
                    category = await _record_failure(key_data, model_used, failure)
                    model_had_transient |= category == "transient"
                    model_all_permanent &= category == "permanent"
                    last_failure = failure
                    continue

                queue: asyncio.Queue[
                    tuple[int, GenerationEvent | None, BaseException | None, bool]
                ] = asyncio.Queue()

                async def _race_provider(
                    idx: int,
                    key_data: dict,
                    *,
                    race_model: str,
                    race_queue: asyncio.Queue[
                        tuple[int, GenerationEvent | None, BaseException | None, bool]
                    ],
                ) -> None:
                    terminal: GenerationEvent | None = None
                    provider = get_provider_for_model(race_model, key_data["api_key"])
                    try:
                        async for event in provider.stream(request, model_name=race_model):
                            if terminal is not None:
                                raise ProviderStreamProtocolError(
                                    f"{provider.provider_name} emitted an event after terminal"
                                )
                            if isinstance(event, TextDelta):
                                race_queue.put_nowait((idx, event, None, False))
                            elif is_terminal_event(event):
                                terminal = event
                            else:
                                raise ProviderStreamProtocolError(
                                    f"Unsupported provider event: {type(event).__name__}"
                                )
                        if terminal is None:
                            raise ProviderStreamProtocolError(
                                f"{provider.provider_name} ended without terminal event"
                            )
                        race_queue.put_nowait((idx, terminal, None, False))
                    except asyncio.CancelledError:
                        raise
                    except BaseException as provider_error:
                        race_queue.put_nowait((idx, None, provider_error, False))
                    finally:
                        race_queue.put_nowait((idx, None, None, True))

                tasks = [
                    asyncio.create_task(
                        _race_provider(
                            index,
                            key_data,
                            race_model=model_used,
                            race_queue=queue,
                        )
                    )
                    for index, key_data in enumerate(keys_to_race)
                ]
                winner_idx: int | None = None
                pre_text_terminals: dict[int, GenerationEvent] = {}
                done: set[int] = set()

                try:
                    while winner_idx is None and len(done) < len(tasks):
                        try:
                            race_idx, race_event, race_error, race_done = await asyncio.wait_for(
                                queue.get(), timeout=30.0
                            )
                        except TimeoutError:
                            timeout_failure = StreamFailed(
                                code=ErrorCode.TIMEOUT,
                                phase=FailurePhase.BEFORE_TEXT,
                                retry=RetryDisposition.TRY_NEXT_KEY,
                                key=KeyDisposition.TRANSIENT_FAILURE,
                                diagnostic="Provider key race timed out before first text",
                            )
                            for index in range(len(keys_to_race)):
                                pre_text_terminals.setdefault(index, timeout_failure)
                            break
                        if race_done:
                            done.add(race_idx)
                            continue
                        if race_error is not None:
                            raise race_error
                        assert race_event is not None
                        if isinstance(race_event, TextDelta):
                            winner_idx = race_idx
                            first_delta = race_event
                            break
                        pre_text_terminals[race_idx] = race_event

                    if winner_idx is None:
                        deferred = next(
                            (
                                event
                                for event in pre_text_terminals.values()
                                if isinstance(event, StreamDeferred)
                            ),
                            None,
                        )
                        if deferred is not None:
                            yield deferred
                            return

                        for index, key_data in enumerate(keys_to_race):
                            terminal = pre_text_terminals.get(index)
                            if isinstance(terminal, StreamCompleted):
                                terminal = StreamFailed(
                                    code=ErrorCode.EMPTY_RESPONSE,
                                    phase=FailurePhase.BEFORE_TEXT,
                                    retry=RetryDisposition.TRY_NEXT_KEY,
                                    key=KeyDisposition.TRANSIENT_FAILURE,
                                    diagnostic="Raced provider completed without visible text",
                                    route=terminal.route,
                                )
                            if not isinstance(terminal, StreamFailed):
                                terminal = StreamFailed(
                                    code=ErrorCode.EMPTY_RESPONSE,
                                    phase=FailurePhase.BEFORE_TEXT,
                                    retry=RetryDisposition.TRY_NEXT_KEY,
                                    key=KeyDisposition.TRANSIENT_FAILURE,
                                    diagnostic="Raced provider ended without visible text",
                                )
                            failed_keys.add(key_data["key_hash"])
                            category = await _record_failure(
                                key_data,
                                model_used,
                                terminal,
                            )
                            model_had_transient |= category == "transient"
                            model_all_permanent &= category == "permanent"
                            last_failure = terminal
                        continue

                    for index, task in enumerate(tasks):
                        if index != winner_idx:
                            task.cancel()
                    winner_key = keys_to_race[winner_idx]
                    await _record_success(winner_key, model_used)
                    yield first_delta

                    while True:
                        try:
                            winner_event = await asyncio.wait_for(
                                queue.get(), timeout=120.0
                            )
                        except TimeoutError as exc:
                            raise ProviderStreamProtocolError(
                                "Winning provider did not emit a terminal event"
                            ) from exc
                        winner_event_idx, terminal_event, winner_error, is_done = winner_event
                        if winner_event_idx != winner_idx:
                            continue
                        if winner_error is not None:
                            raise winner_error
                        if is_done:
                            raise ProviderStreamProtocolError(
                                "Winning provider ended before terminal delivery"
                            )
                        assert terminal_event is not None
                        if isinstance(terminal_event, TextDelta):
                            yield terminal_event
                            continue
                        if isinstance(terminal_event, StreamDeferred):
                            raise ProviderStreamProtocolError(
                                "Winning provider deferred after visible text"
                            )
                        if (
                            isinstance(terminal_event, StreamFailed)
                            and terminal_event.phase is not FailurePhase.AFTER_TEXT
                        ):
                            raise ProviderStreamProtocolError(
                                "Winning provider reported BEFORE_TEXT after text"
                            )
                        yield terminal_event
                        return
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

            overall_had_transient |= model_had_transient

            if is_opencode_model(preferred_model):
                gemini_fallback = _get_opencode_gemini_fallback().get(
                    preferred_model,
                    _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
                )
                if gemini_fallback not in attempted_models and gemini_fallback not in model_queue:
                    model_queue.append(gemini_fallback)

            if model_had_transient:
                transient_fallback = self._pick_transient_fallback_model(
                    preferred_model,
                    None,
                )
                if (
                    transient_fallback
                    and transient_fallback not in attempted_models
                    and transient_fallback not in model_queue
                ):
                    model_queue.append(transient_fallback)

            if model_all_permanent and last_failure is not None:
                for fallback in _ordered_gemini_fallback_models(preferred_model):
                    if fallback not in attempted_models and fallback not in model_queue:
                        model_queue.append(fallback)

        if (
            request.allow_deferred
            and request.workload is not Workload.DEFERRED_RETRY
            and scope.user_id
            and scope.chat_id
            and overall_had_transient
        ):
            try:
                from app.deferred_response import enqueue_deferred_generation

                task_id = await enqueue_deferred_generation(
                    user_id=scope.user_id,
                    chat_id=scope.chat_id,
                    history=deferred_history_from_request(request),
                    model_name=request.models[0],
                    system_instruction=request.system_instruction,
                )
                if task_id:
                    yield StreamDeferred(task_id=task_id)
                    return
            except Exception as exc:
                logging.warning("Typed deferred queue fallback failed: %s", exc)

        if last_failure is not None:
            yield StreamFailed(
                code=last_failure.code,
                phase=FailurePhase.BEFORE_TEXT,
                retry=(
                    RetryDisposition.RETRY_LATER
                    if overall_had_transient
                    else RetryDisposition.DO_NOT_RETRY
                ),
                key=last_failure.key,
                diagnostic=last_failure.diagnostic,
                route=last_failure.route,
            )
            return

        yield StreamFailed(
            code=ErrorCode.KEYS_EXHAUSTED,
            phase=FailurePhase.BEFORE_TEXT,
            retry=RetryDisposition.RETRY_LATER,
            key=KeyDisposition.EXHAUSTED,
            diagnostic="All provider routes exhausted without a terminal response",
        )

    def _pick_transient_fallback_model(
        self,
        failed_model: str,
        use_openrouter: bool | None,
    ) -> str | None:
        """Pick a lighter fallback model for transient (503) errors.

        Maps heavy models to their lite counterparts. Returns None if
        the failed model is already the lightest available.
        """
        # Grounding stays on the Gemini 2.5 family because free-tier AI Studio
        # keys do not support Gemini 3+ Google Search grounding. Other Gemini
        # requests use the complete ordered runtime fallback chain.
        _GEMINI_CASCADE = {
            GEMINI_GROUNDING_MODEL: GEMINI_GROUNDING_FALLBACK_MODEL,
        }

        # Opencode Go: cascade to Gemini via the cross-provider fallback map
        if is_opencode_model(failed_model):
            gemini_fallback = _get_opencode_gemini_fallback().get(
                failed_model,
                _setting("DEFAULT_MODEL", GEMINI_PRIMARY_MODEL),
            )
            if gemini_fallback in _runtime_gemini_models():
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
        if fallback is None:
            ordered_fallbacks = _ordered_gemini_fallback_models(gemini_failed_model)
            fallback = ordered_fallbacks[0] if ordered_fallbacks else None
        if fallback:
            if fallback in (GEMINI_GROUNDING_FALLBACK_MODEL,):
                return fallback
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
