"""
Inline mode handlers — cross-chat bot interaction via @mention.

Flow:
  1. User types ``@gemaibotv2 <query>`` in any Telegram chat.
  2. Bot instantly returns 3 InlineQueryResultArticle entries (tone variants),
     each seeded with a styled "thinking…" placeholder as its initial content.
  3. User selects the desired tone → placeholder is posted to the chat.
  4. ``handle_chosen_inline_result`` captures ``inline_message_id`` + query
     + chosen tone, then fires ``_generate_and_edit_inline`` as a background task.
  5. ``_generate_and_edit_inline``:
       a) Calls ``tavily_search_agent`` (QnA mode) for a real-time context snippet.
       b) Injects context + tone hint into the system prompt of
          ``gemini-3.1-flash-lite-preview`` via ``_get_ai_response_with_routing``.
       c) Converts the Markdown answer to Telegram HTML and edits the inline
          placeholder message in-place using ``bot.edit_message_text(inline_message_id=…)``.

Prerequisites (one-time BotFather setup):
  - ``/setinline``         — enable inline mode; set placeholder text.
  - ``/setinlinefeedback`` — set to **100%** so the bot receives
                             ``ChosenInlineResult`` updates with ``inline_message_id``.
"""

import asyncio
import html as _html
import logging
import time
import uuid
from datetime import UTC, datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from app.config import settings
from app.errors import is_error_message
from app.metrics import metrics_collector
from app.repos.settings_repo import get_global_setting
from app.utils.api_logger import api_logger
from app.utils.text_format import markdown_to_html, strip_formatting

# ── Constants ────────────────────────────────────────────────────────────────

_INLINE_MODEL = "gemini-3.1-flash-lite-preview"

# Placeholder sent to the chat immediately upon selection (before generation).
# Resolved dynamically at runtime via context.bot.first_name.


def _placeholder_html(bot_name: str) -> str:
    return f"⚡️ <b>{_html.escape(bot_name)}</b> генерирует ответ…"


# (result_id, display_label, system_tone_hint)
_TONES: list[tuple[str, str, str]] = [
    (
        "formal",
        "📋 Формальный ответ",
        "Отвечай строго, профессионально и по делу. Только факты, без юмора.",
    ),
    (
        "friendly",
        "😊 Дружеский ответ",
        "Отвечай тепло, понятно и неформально, как близкий друг. Допускай эмодзи.",
    ),
    (
        "sarcastic",
        "😏 Саркастичный ответ",
        "Отвечай с приятной иронией и лёгким сарказмом, оставаясь при этом полезным.",
    ),
]

# Prevent fire-and-forget background tasks from being garbage-collected.
_bg_tasks: set[asyncio.Task] = set()

# Inline keyboard attached to every result — Telegram Bot API REQUIRES this
# for ChosenInlineResult to include `inline_message_id`.  Without it the bot
# cannot edit the placeholder in-place.  The button itself is cosmetic and
# gets replaced once the final response is ready.
_LOADING_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Генерация…", callback_data="inline_noop")]])

# ── Retry store ──────────────────────────────────────────────────────────────
# Keyed by short UUID, stores params needed to re-run _generate_and_edit_inline.
# Entries auto-expire; we prune on every new insert. TTL = 5 minutes.
_RETRY_TTL_S = 300.0
_retry_store: dict[str, dict] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _tone_display(tone_id: str) -> str:
    for tid, label, _ in _TONES:
        if tid == tone_id:
            return label
    return tone_id


def _tone_hint(tone_id: str) -> str:
    for tid, _, hint in _TONES:
        if tid == tone_id:
            return hint
    return ""


# ── Public handlers ───────────────────────────────────────────────────────────


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return 3 tone-variant placeholder results instantly for any non-empty query."""
    query = update.inline_query
    if not query:
        return

    user_query = query.query.strip()
    bot_name = context.bot.first_name or "Bot"
    placeholder = _placeholder_html(bot_name)

    if not user_query:
        # Guide the user when no text has been typed yet.
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    id="hint",
                    title="🤖 Введите запрос после @бота…",
                    description="Например: какая погода в Москве?",
                    input_message_content=InputTextMessageContent(
                        message_text=placeholder,
                        parse_mode="HTML",
                    ),
                    reply_markup=_LOADING_KEYBOARD,
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    results = [
        InlineQueryResultArticle(
            id=tone_id,
            title=label,
            description=user_query[:120],
            input_message_content=InputTextMessageContent(
                message_text=placeholder,
                parse_mode="HTML",
            ),
            reply_markup=_LOADING_KEYBOARD,
        )
        for tone_id, label, _ in _TONES
    ]

    # cache_time=0 ensures each new character triggers a fresh result list.
    await query.answer(results, cache_time=0, is_personal=True)


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture ChosenInlineResult metadata and launch background generation."""
    chosen = update.chosen_inline_result
    if not chosen:
        return

    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        logging.warning(
            "Inline: no inline_message_id received — ensure /setinlinefeedback is set to 100%% in BotFather."
        )
        return

    user_query = chosen.query.strip()
    tone_id = chosen.result_id
    user_id = chosen.from_user.id if chosen.from_user else None

    if not user_query or tone_id == "hint":
        bot_name = context.bot.first_name or "бота"
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"❌ <b>Ошибка:</b> Пустой запрос.\nВведите текст после @{bot_name} (например, <i>какая сегодня погода?</i>)",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception as e:
            logging.error("Inline: Failed to edit empty query hint: %s", e)
        return

    task = asyncio.create_task(
        _generate_and_edit_inline(
            bot=context.bot,
            inline_message_id=inline_message_id,
            user_query=user_query,
            tone_id=tone_id,
            user_id=user_id,
        )
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ── Fast 3-way Race Requests for inline generation ──────────────────────────


async def _stream_inline_fast(
    preferred_model: str,
    history: list,
    system_instruction: str | None,
    user_id: int | None,
    max_rounds: int = 4,
) -> str | None:
    """3-way Race Requests accumulator optimised for inline speed.

    Fires 3 keys simultaneously per round. The first to yield a real chunk
    wins; the other two are cancelled instantly. Zero sleep between rounds.
    With 15+ keys and generous gemini-3.1-flash-lite RPD limits, burning
    3 simultaneous slots per round is essentially free operationally.

    Returns:
        Accumulated full-response text, or None if all rounds fail.
    """
    from app.agent_use_cases import AgentRequestUseCase
    from app.providers.base import get_provider_for_model
    from app.repos.keys import get_key_status_manager

    use_case = AgentRequestUseCase()
    status_mgr = get_key_status_manager()
    failed_keys: set[str] = set()

    class _End:
        """Sentinel: producer puts this when its stream finishes or is cancelled."""

        __slots__ = ("key_hash",)

        def __init__(self, kh: str) -> None:
            self.key_hash = kh

    for _round in range(max_rounds):
        # ── Resolve up to 3 distinct keys for this round ────────────────────
        keys: list[dict] = []
        resolved_model: str | None = None
        for _ in range(3):
            kd, mdl, _ = await use_case.resolve_ai_request(
                preferred_model,
                excluded_key_hashes=failed_keys | {k["key_hash"] for k in keys},
            )
            if kd and mdl:
                keys.append(kd)
                resolved_model = mdl
            else:
                break  # No more available keys

        if not keys or not resolved_model:
            return None  # No keys available at all

        # Read thinking level dynamically — admin can change via /set_inline_thinking
        # without restarting the container. Falls back to env-var default.
        thinking_level = await get_global_setting(
            "inline_thinking_level", settings.INLINE_THINKING_LEVEL
        )

        q: asyncio.Queue = asyncio.Queue()

        async def _race(kd: dict, mod: str = resolved_model, _q: asyncio.Queue = q) -> None:  # type: ignore[assignment]  # noqa: B023
            kh = kd["key_hash"]
            try:
                prov = get_provider_for_model(mod, kd["api_key"])
                async for chunk in prov.stream_response(  # type: ignore[attr-defined]
                    history=history,
                    model_name=mod,
                    system_instruction=system_instruction,
                    thinking_level=thinking_level,
                    timeout=18.0,
                ):
                    await _q.put((kh, chunk, None))
            except asyncio.CancelledError:
                pass  # Loser cancelled normally — no sentinel needed
            except Exception as exc:
                await _q.put((kh, None, exc))  # noqa: B023
                return
            await _q.put((kh, _End(kh), None))

        tasks: dict[str, asyncio.Task] = {
            kd["key_hash"]: asyncio.create_task(_race(kd)) for kd in keys
        }
        winner_kh: str | None = None
        chunks: list[str] = []
        errors: dict[str, Exception] = {}

        # ── Phase 1: find the first key to yield a real chunk ────────────────
        try:
            while winner_kh is None and len(errors) < len(keys):
                try:
                    kh, chunk, exc = await asyncio.wait_for(q.get(), timeout=20.0)
                except TimeoutError:
                    failed_keys.update(kd["key_hash"] for kd in keys)
                    break

                if exc is not None:
                    errors[kh] = exc
                    failed_keys.add(kh)
                    continue
                if isinstance(chunk, _End):
                    errors[kh] = RuntimeError("stream ended without chunks")
                    failed_keys.add(kh)
                    continue
                if chunk and not is_error_message(chunk):
                    winner_kh = kh
                    chunks.append(chunk)
                    # Cancel all losers immediately
                    for k, t in tasks.items():
                        if k != winner_kh and not t.done():
                            t.cancel()
        except Exception:
            pass  # Unexpected queue/task error — fall through to None check

        if winner_kh is None:
            for t in tasks.values():
                if not t.done():
                    t.cancel()
            continue  # Next round with fresh keys

        # Record winner health (non-critical)
        try:
            await status_mgr.record_success(winner_kh, resolved_model)
            await use_case.increment_key_usage(winner_kh, resolved_model, False)
        except Exception:
            pass

        # ── Phase 2: drain remaining chunks from winner ──────────────────────
        try:
            while True:
                try:
                    kh, chunk, exc = await asyncio.wait_for(q.get(), timeout=18.0)
                except TimeoutError:
                    logging.warning("Inline: winner drain timed out after 18s")
                    break
                if kh != winner_kh:
                    continue  # Stale item from cancelled loser — discard
                if exc is not None:
                    logging.warning("Inline: winner stream failed mid-flight: %s", exc)
                    break
                if isinstance(chunk, _End):
                    break  # Clean completion
                if chunk:
                    chunks.append(chunk)
        finally:
            for t in tasks.values():
                if not t.done():
                    t.cancel()

        result = "".join(chunks)
        if result.strip() and not is_error_message(result):
            return result

        # Winner produced error-tagged text — mark all keys failed and retry
        failed_keys.update(kd["key_hash"] for kd in keys)

    return None  # All rounds exhausted


# ── Background generation ─────────────────────────────────────────────────────


async def _generate_and_edit_inline(
    bot,
    inline_message_id: str,
    user_query: str,
    tone_id: str,
    user_id: int | None,
) -> None:
    """
    Core async pipeline:

    1. Tavily QnA search for real-time context (best-effort, 8 s timeout).
    2. Call ``gemini-3.1-flash-lite-preview`` with context + tone in system prompt.
    3. Convert Markdown response to Telegram HTML.
    4. Edit the placeholder inline message in-place.
    """
    from app.prompt_registry import FORMATTING_RULES_COMPACT
    from app.search_services import tavily_search_agent

    tone_sys_hint = _tone_hint(tone_id)
    tone_label = _tone_display(tone_id)
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    # ── Step 1: Fetch search context (non-blocking, best-effort) ─────────────
    search_context = ""
    try:
        search_result = await asyncio.wait_for(
            tavily_search_agent(user_query, search_type="qna", user_id=user_id),
            timeout=8.0,
        )
        if isinstance(search_result, dict) and search_result.get("type") == "answer":
            search_context = (search_result.get("content") or "").strip()
    except Exception as search_err:
        logging.warning("Inline: Tavily QnA failed, proceeding without context: %s", search_err)

    # ── Step 2: Build system prompt ───────────────────────────────────────────
    system_instruction = (
        f"[system: current_utc_date={today}]\n"
        f"Тон ответа: {tone_sys_hint}\n"
        "Ты — ассистент в инлайн-режиме Telegram. "
        "Пользователь задаёт вопрос прямо из переписки с другим человеком — "
        "отвечай КРАТКО и по существу (не более 3–4 абзацев).\n\n"
        f"{FORMATTING_RULES_COMPACT}\n"
    )
    if search_context:
        system_instruction += (
            f"\n[Актуальная информация из интернета (используй при необходимости)]:\n{search_context[:2000]}"
        )

    history = [{"role": "user", "parts": [user_query]}]

    # ── Step 3: Generate (3-way Race Requests, up to 4 rounds = 12 key slots) ─
    # For gemini-3.1-flash-lite-preview with 15+ keys at hundreds RPD each,
    # burning 3 simultaneous slots is operationally free and minimises TTFR.
    _GEN_TIMEOUT_S = 22.0
    _gen_start = time.monotonic()
    final_answer: str | None = None
    _gen_timed_out = False
    _log_start = api_logger.log_request(
        "gemini_inline",
        model=_INLINE_MODEL,
        query_length=len(user_query),
        tone=tone_id,
    )
    try:
        final_answer = await asyncio.wait_for(
            _stream_inline_fast(
                preferred_model=_INLINE_MODEL,
                history=history,
                system_instruction=system_instruction,
                user_id=user_id,
                max_rounds=4,
            ),
            timeout=_GEN_TIMEOUT_S,
        )
    except TimeoutError:
        _gen_timed_out = True
        logging.warning(
            "Inline: Generation timed out after %.0fs for query '%s'",
            _GEN_TIMEOUT_S,
            user_query[:60],
        )
    except Exception as gen_err:
        logging.error(
            "Inline: Generation failed for query '%s': %s",
            user_query[:60],
            gen_err,
            exc_info=True,
        )
    finally:
        _gen_success = bool(final_answer and not is_error_message(final_answer))
        api_logger.log_response(
            "gemini_inline",
            _log_start,
            success=_gen_success,
            model=_INLINE_MODEL,
            response_length=len(final_answer or ""),
        )

    # Record metrics (we're already in a background task — awaiting is safe)
    await metrics_collector.record_api_call("gemini_inline", _INLINE_MODEL, user_id=user_id)
    await metrics_collector.record_request(
        "inline",
        response_time=time.monotonic() - _gen_start,
        success=_gen_success,
        user_id=user_id,
    )

    # ── Step 4: Format and edit inline message ────────────────────────────────
    # A tagged error response (e.g. quota exhausted) is treated as a failure:
    # we show the clean error message instead of rendering the raw tag string.
    _is_api_error = bool(final_answer and is_error_message(final_answer))
    if final_answer and final_answer.strip() and not _is_api_error:
        header = f"<b>{_html.escape(tone_label)}</b> · <code>{_html.escape(user_query[:60])}</code>\n\n"
        body = markdown_to_html(final_answer.strip())
        formatted = header + body
        # Telegram inline messages: hard 4096-char limit.
        if len(formatted) > 4000:
            formatted = formatted[:3997] + "…"
    else:
        from app.errors import strip_error_tag
        if _is_api_error and final_answer:
            formatted = strip_error_tag(final_answer)
        else:
            formatted = "⏰ Модель не успела ответить вовремя." if _gen_timed_out else "❌ Не удалось получить ответ."

    # On failure, attach a retry button so the user can re-trigger generation.
    # is_failure is True in two cases:
    #   1. AI returned nothing (timeout, exception, empty string)
    #   2. AI returned a tagged error string (e.g. "🚧 Все ключи исчерпаны")
    #      — is_error_message() detects zero-width ErrorCode tags embedded by tag_error()
    reply_markup: InlineKeyboardMarkup | None = None
    is_failure = not (final_answer and final_answer.strip()) or bool(final_answer and is_error_message(final_answer))
    if is_failure:
        retry_id = _store_retry_params(
            user_query=user_query,
            tone_id=tone_id,
            user_id=user_id,
        )
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Повторить", callback_data=f"inl_retry:{retry_id}")]]
        )
    else:
        reply_markup = InlineKeyboardMarkup([])  # strip loading indicator

    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=formatted,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception as edit_err:
        logging.error(
            "Inline: Failed to edit inline message %s: %s",
            inline_message_id,
            edit_err,
        )
        # Last-resort: strip HTML tags and retry as plain text.
        try:
            plain = strip_formatting(formatted)[:4000]
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=plain or "Ошибка генерации ответа.",
                reply_markup=reply_markup,
            )
        except Exception as fallback_err:
            logging.error("Inline: Plain-text fallback also failed: %s", fallback_err)


# ── Retry store helpers ───────────────────────────────────────────────────────


def _store_retry_params(
    user_query: str,
    tone_id: str,
    user_id: int | None,
) -> str:
    """Store retry params and return a short ID (fits in callback_data)."""
    # Prune expired entries
    import time as _time

    now = _time.monotonic()
    expired = [k for k, v in _retry_store.items() if now - v["ts"] > _RETRY_TTL_S]
    for k in expired:
        _retry_store.pop(k, None)

    # Hard cap: evict oldest entries if store grows beyond 500 items.
    # Protects against memory accumulation under sustained inline traffic
    # where inserts outpace the 5-minute TTL eviction.
    _STORE_MAX = 500
    if len(_retry_store) >= _STORE_MAX:
        oldest_keys = sorted(_retry_store, key=lambda k: _retry_store[k]["ts"])[: len(_retry_store) - _STORE_MAX + 100]
        for k in oldest_keys:
            _retry_store.pop(k, None)

    retry_id = uuid.uuid4().hex[:12]
    _retry_store[retry_id] = {
        "query": user_query,
        "tone": tone_id,
        "user_id": user_id,
        "ts": now,
    }
    return retry_id


# ── Retry callback handler ────────────────────────────────────────────────────


async def handle_inline_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 🔄 Повторить button press on failed inline messages."""
    import time as _time

    query = update.callback_query
    if not query:
        return

    await query.answer()  # dismiss the spinner immediately

    data = query.data or ""
    if not data.startswith("inl_retry:"):
        return

    retry_id = data.split(":", 1)[1]
    entry = _retry_store.pop(retry_id, None)

    if not entry or (_time.monotonic() - entry["ts"] > _RETRY_TTL_S):
        # Expired or unknown — edit with a polite message
        try:
            await query.edit_message_text(
                "⏳ Запрос устарел. Пожалуйста, вызовите бот заново.",
            )
        except Exception:
            pass
        return

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        return

    # Show loading state
    bot_name = context.bot.first_name or "Bot"
    try:
        await query.edit_message_text(
            text=_placeholder_html(bot_name),
            parse_mode="HTML",
            reply_markup=_LOADING_KEYBOARD,
        )
    except Exception:
        pass

    # Re-run generation as a background task
    task = asyncio.create_task(
        _generate_and_edit_inline(
            bot=context.bot,
            inline_message_id=inline_message_id,
            user_query=entry["query"],
            tone_id=entry["tone"],
            user_id=entry["user_id"],
        )
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
