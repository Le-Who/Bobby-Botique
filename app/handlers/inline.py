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
       a) Calls ``gemini-2.5-flash-lite`` with Google Search Grounding enabled.
       b) Grounding citations (when available) are appended as an expandable
          blockquote below the answer.
       c) Converts the Markdown answer to Telegram HTML and edits the inline
          placeholder message in-place using ``bot.edit_message_text(inline_message_id=…)``.

Image intent routing (5 modes, auto-selected):
  ⚡ Турбо     (zimage)      — fast, high-quality, general use
  🧠 Умный    (gptimage)    — GPT Image 1, prompt auto-enhanced by model
  🎨 Арт      (qwen-image)  — Qwen stylized / avatars
  🅰️ Мем/Текст (wan-image)  — auto-routed when quoted text detected in prompt
  🪄 Изменить фото (klein)  — auto-routed by image editing intent keywords

Prerequisites (one-time BotFather setup):
  - ``/setinline``         — enable inline mode; set placeholder text.
  - ``/setinlinefeedback`` — set to **100%** so the bot receives
                             ``ChosenInlineResult`` updates with ``inline_message_id``.
"""

import asyncio
import contextlib
import html as _html
import io
import logging
import re
import time
import uuid
from datetime import UTC, datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputFile,
    InputMediaPhoto,
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

_INLINE_MODEL = "gemini-2.5-flash-lite"

# Outer timeout for the entire generation pipeline.
_GEN_TIMEOUT_S = 55.0
# Seconds after which we edit the placeholder to show a progress message.
_GEN_PROGRESS_AFTER_S = 20.0

# ── Image intent detection ────────────────────────────────────────────────────
# Matches: "нарисуй", "draw", "сгенерируй картинку", "imagine", "изобрази", etc.
_IMAGE_INTENT_RE = re.compile(
    r"(?:наруй|нарисуй|draw|draws|изобрази|imagine|сгенерируй\s*(?:картинку|изображение|фото|image)|portrait|generate\s*image)",
    re.IGNORECASE,
)

# ── Smart-routing patterns ────────────────────────────────────────────────────
# Quoted text in prompt → wan-image (renders text accurately on images)
_QUOTED_TEXT_RE = re.compile(r'["\u00ab\u00bb\u201c\u201d].+?["\u00ab\u00bb\u201c\u201d]|\'.+?\'', re.DOTALL)

# Image editing intent → klein (FLUX.2 Klein 4B, edit/inpaint capable)
_IMAGE_EDIT_INTENT_RE = re.compile(
    r"(?:измени|отредактируй|добавь|убери|замени|перекраси|edit|change|add|remove|replace|modify|inpaint)"
    r"\s+(?:фото|фотку|картинку|изображение|image|photo|picture)",
    re.IGNORECASE,
)

# Image model variants surfaced in inline results.
# Format: (result_id_prefix, display_title, pollinations_model)
# Approved set (2026-04-13):
#   ⚡ Турбо    — Z-Image Turbo (6B Flux + 2× upscaling), fast & crisp
#   🤖 Умный   — GPT Image 1 Mini, prompt auto-enhanced by the model
#   🎨 Арт     — Qwen Image Plus, stylized / avatar quality
#   🅰️ Мем    — Wan 2.7 Image, accurate text rendering on images
#   🔷 Изменить — FLUX.2 Klein 4B, auto-routed only (hidden from menu)
# Format: (result_id_prefix, display_title, pollinations_model, placeholder_url)
# Each model gets a UNIQUE color so the 2×2 grid is visually distinct.
_IMAGE_MODELS: list[tuple[str, str, str, str]] = [
    (
        "img_turbo",
        "⚡ Турбо — быстро и красиво",
        "zimage",
        "https://placehold.co/800x450/0d1117/58a6ff.jpg?text=Turbo",
    ),
    (
        "img_smart",
        "🤖 Умный — бот улучшит промпт",
        "gptimage",
        "https://placehold.co/800x450/0d2b0d/00e676.jpg?text=Smart+AI",
    ),
    (
        "img_art",
        "🎨 Арт / Аватарка — стилизация",
        "qwen-image",
        "https://placehold.co/800x450/1a0d2e/cc77ff.jpg?text=Art+Style",
    ),
    (
        "img_meme",
        "🅰️ Мем / Текст — точный текст",
        "wan-image",
        "https://placehold.co/800x450/2b1500/ffaa00.jpg?text=Meme+Text",
    ),
]
# Klein is NOT shown in the inline menu — it is auto-routed when user attaches
# an image with an edit intent keyword.
_IMG_KLEIN_ID    = "img_edit"
_IMG_KLEIN_MODEL = "klein"
_IMG_KLEIN_PLACEHOLDER = "https://placehold.co/800x450/1a1a0d/ffe066.jpg?text=Edit+Photo"


# Board intent prefix — compiled once; shared by query and result handlers.
_BOARD_PREFIX_RE = re.compile(r"^(?:доска|board|трекер)\s*:\s*", re.IGNORECASE)

# Model emoji map reused in the generated image caption.
_MODEL_EMOJI: dict[str, str] = {
    "zimage": "⚡",
    "seedream5": "🌱",
    "seedream": "🌿",
    "gptimage": "🧠",
    "gptimage-large": "💎",
    "qwen-image": "🎨",
    "wan-image": "🅰️",
    "wan-image-pro": "🅰️",
    "kontext": "🖋️",
    "klein": "🪄",
    "flux": "✨",
    "grok-imagine": "🚀",
    "grok-imagine-pro": "💠",
}


def _get_model_emoji(model: str) -> str:
    return _MODEL_EMOJI.get(model, "🎨")


# ── Tabbed response store ──────────────────────────────────────────────────────
# In-memory TTL dict for segmented responses. Keyed by inline_message_id.
# Entries expire after _TABS_TTL_S. Capped at _TABS_STORE_MAX entries (FIFO eviction).
_TABS_TTL_S = 600.0  # 10 minutes
_TABS_STORE_MAX = 256
_inline_tabs_store: dict[str, dict] = {}  # {imid: {tldr, details, sources, created}}


def _tabs_store_put(inline_message_id: str, segments: dict) -> None:
    """Insert segmented response; evict oldest entries when cap is reached."""
    now = time.monotonic()
    # Evict expired first
    expired = [k for k, v in _inline_tabs_store.items() if now - v["created"] > _TABS_TTL_S]
    for k in expired:
        _inline_tabs_store.pop(k, None)
    # Enforce cap
    if len(_inline_tabs_store) >= _TABS_STORE_MAX:
        oldest = sorted(_inline_tabs_store, key=lambda k: _inline_tabs_store[k]["created"])
        for k in oldest[: len(oldest) // 4 + 1]:
            _inline_tabs_store.pop(k, None)
    _inline_tabs_store[inline_message_id] = {**segments, "created": now}


def _tabs_store_get(inline_message_id: str) -> dict | None:
    """Return segments for inline_message_id, or None if missing/expired."""
    entry = _inline_tabs_store.get(inline_message_id)
    if not entry:
        return None
    if time.monotonic() - entry["created"] > _TABS_TTL_S:
        _inline_tabs_store.pop(inline_message_id, None)
        return None
    return entry


# ── User-facing inline UX strings ────────────────────────────────────────────
# Kept as named constants for easy future extraction to i18n.


def _placeholder_html(bot_name: str) -> str:
    # Inline always uses Gemini Search Grounding → "searching" is accurate UX
    return f"🔎 <b>{_html.escape(bot_name)}</b> ищет в интернете…"


def _progress_search_done_html(bot_name: str) -> str:
    return f"🧠 <b>{_html.escape(bot_name)}</b> собрал информацию, теперь генерирует ответ…"


def _progress_delayed_html(bot_name: str) -> str:
    return f"⏳ <b>{_html.escape(bot_name)}</b> задерживается…"


_TIMEOUT_ERROR = "⏰ Модель не успела ответить вовремя. Нажмите «Повторить» ниже."
_GENERATION_ERROR = "❌ Не удалось получить ответ."
_FALLBACK_ERROR = "Ошибка генерации ответа."


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
    """Return instant placeholder results for any non-empty query.

    Intent routing:
      - Image intent (нарисуй / draw / etc.) → 2 InlineQueryResultPhoto (model variants)
      - Board intent (доска: / board:)        → 1 InlineQueryResultArticle (board template)
      - Default                               → 3 InlineQueryResultArticle (tone variants)
    """
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

    # ── Image intent ──────────────────────────────────────────────────────────
    if _IMAGE_INTENT_RE.search(user_query):
        # Strip the intent verb from the prompt ("нарисуй кота" → "кота")
        stripped_prompt = _IMAGE_INTENT_RE.sub("", user_query, count=1).strip(" ,-.!") or user_query

        # ── Smart auto-routing ────────────────────────────────────────────────
        has_quoted_text = bool(_QUOTED_TEXT_RE.search(stripped_prompt))
        has_edit_intent = bool(_IMAGE_EDIT_INTENT_RE.search(user_query))

        if has_edit_intent:
            # Edit/inpaint mode: show only klein
            routed_models: list[tuple[str, str, str, str]] = [
                (_IMG_KLEIN_ID, "🪄 Изменить фото", _IMG_KLEIN_MODEL, _IMG_KLEIN_PLACEHOLDER)
            ]
            auto_hint = "✏️ Режим редактирования (Klein)"
        elif has_quoted_text:
            # Meme/text mode: wan-image first for text accuracy
            meme_entry = next((m for m in _IMAGE_MODELS if m[0] == "img_meme"), None)
            rest = [m for m in _IMAGE_MODELS if m[0] != "img_meme"]
            routed_models = ([meme_entry] if meme_entry else []) + rest
            auto_hint = "🅰️ Обнаружен текст → авто-выбран Мем-режим"
        else:
            routed_models = _IMAGE_MODELS
            auto_hint = ""

        results_img = [
            InlineQueryResultPhoto(
                id=result_id,
                photo_url=placeholder_url,
                thumbnail_url=placeholder_url,
                title=title,
                description=(
                    f"{auto_hint} · {stripped_prompt[:70]}" if auto_hint else stripped_prompt[:100]
                ),
                caption=(
                    f"🎨 <b>Запрос:</b> {_html.escape(stripped_prompt[:200])}"
                    + (f"\n<i>{_html.escape(auto_hint)}</i>" if auto_hint else "")
                    + "\n⏳ Генерация…"
                ),
                parse_mode="HTML",
                reply_markup=_LOADING_KEYBOARD,
            )
            for result_id, title, _, placeholder_url in routed_models
        ]
        await query.answer(results_img, cache_time=0, is_personal=True)
        return

    # ── Board / Topic Aggregator intent ───────────────────────────────────────
    if _BOARD_PREFIX_RE.match(user_query):
        topic = _BOARD_PREFIX_RE.sub("", user_query).strip() or user_query
        board_init_html = (
            f"📋 <b>{_html.escape(topic)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Отвечайте (reply) на это сообщение, чтобы добавить свои идеи.</i>\n\n"
            "Пока ничего не предложено."
        )
        board_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Доска активирована", callback_data="board_link:pending")]]
        )
        results_board = [
            InlineQueryResultArticle(
                id="board",
                title=f"📋 Создать доску: {topic[:60]}",
                description="Участники смогут добавлять идеи через reply",
                input_message_content=InputTextMessageContent(
                    message_text=board_init_html,
                    parse_mode="HTML",
                ),
                reply_markup=board_keyboard,
            )
        ]
        await query.answer(results_board, cache_time=0, is_personal=True)
        return

    # ── Default: 3 tone variants ──────────────────────────────────────────────
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
    """Capture ChosenInlineResult metadata and launch the correct background task.

    Dispatch table:
      result_id starts with "img_"  → _generate_and_swap_media (Photo Placeholder Swap)
      result_id == "board"          → create board in DB, register inline_message_id
      otherwise                     → _generate_and_edit_inline (text + optional tabs)
    """
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
    result_id = chosen.result_id
    user_id = chosen.from_user.id if chosen.from_user else None

    # ── Guard: empty / hint ───────────────────────────────────────────────────
    if not user_query or result_id == "hint":
        bot_name = context.bot.first_name or "бота"
        _empty_hint = (
            f"❌ <b>Ошибка:</b> Пустой запрос.\n"
            f"Введите текст после @{bot_name} "
            f"(например, <i>какая сегодня погода?</i>)"
        )
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=_empty_hint,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception as e:
            logging.error("Inline: Failed to edit empty query hint: %s", e)
        return

    from app.utils.background_tasks import get_task_manager

    # ── Image generation path ─────────────────────────────────────────────────
    if result_id.startswith("img_"):
        # Include klein (auto-routed, hidden from menu) in model lookup
        all_known_models = _IMAGE_MODELS + [(_IMG_KLEIN_ID, "🪄 Изменить фото", _IMG_KLEIN_MODEL)]
        prov_model = next(
            (m for rid, _, m in all_known_models if rid == result_id),
            "zimage",  # fallback to Турбо
        )
        # Extract clean prompt: remove intent verb prefix
        prompt = _IMAGE_INTENT_RE.sub("", user_query, count=1).strip(" ,-.!") or user_query
        # gptimage (Умный mode) supports prompt enhancement from Pollinations
        enhance_prompt = result_id == "img_smart"
        get_task_manager().submit(
            _generate_and_swap_media(
                bot=context.bot,
                inline_message_id=inline_message_id,
                prompt=prompt,
                model=prov_model,
                user_id=user_id,
                enhance_prompt=enhance_prompt,
            )
        )
        return

    # ── Board / Topic Aggregator path ─────────────────────────────────────────
    if result_id == "board":
        topic = _BOARD_PREFIX_RE.sub("", user_query).strip() or user_query
        get_task_manager().submit(
            _init_board_async(
                bot=context.bot,
                inline_message_id=inline_message_id,
                topic=topic,
                creator_id=user_id or 0,
            )
        )
        return

    # ── Default text generation path ──────────────────────────────────────────
    get_task_manager().submit(
        _generate_and_edit_inline(
            bot=context.bot,
            inline_message_id=inline_message_id,
            user_query=user_query,
            tone_id=result_id,
            user_id=user_id,
        )
    )


# ── Photo Placeholder Swap ────────────────────────────────────────────────────


async def _generate_and_swap_media(
    bot,
    inline_message_id: str,
    prompt: str,
    model: str,
    user_id: int | None,
    enhance_prompt: bool = False,
) -> None:
    """Generate an image via Pollinations and swap the inline placeholder photo.

    Flow:
      1. Call PollinationsProvider.generate() — returns raw JPEG/PNG bytes.
      2. Upload bytes to the admin chat via send_photo() to obtain a stable file_id.
         Telegram's Bot API does NOT accept InputFile(BytesIO(...)) for
         edit_message_media on inline messages (inline_message_id path) —
         only file_id strings or HTTP URLs are valid there.
         The temp message is deleted immediately after the file_id is extracted.
      3. Use the minted file_id in InputMediaPhoto for edit_message_media.
      4. On any failure, edit caption to show an error.
    """
    import contextlib

    from app.config import settings as _settings
    from app.providers.pollinations import get_pollinations_provider

    _gen_start = time.monotonic()
    provider = get_pollinations_provider()

    try:
        result = await provider.generate(prompt=prompt, model=model, seed=-1, enhance=enhance_prompt)
    except Exception as exc:
        logging.error("Inline image: generation exception: %s", exc, exc_info=True)
        result = None

    elapsed = time.monotonic() - _gen_start

    if result and result.success and result.images:
        image_bytes = result.images[0]
        caption = (
            f"🎨 <b>{_html.escape(prompt[:200])}</b>"
            f"\n<i>{_get_model_emoji(model)} {model} • {elapsed:.1f}s</i>"
        )

        # ── Step 1: Mint a file_id by uploading bytes to the admin chat ───
        # edit_message_media with inline_message_id only accepts a file_id
        # string or a URL — InputFile(BytesIO(...)) multipart is rejected.
        file_id: str | None = None
        temp_msg = None
        try:
            temp_msg = await bot.send_photo(
                chat_id=_settings.ADMIN_ID,
                photo=InputFile(io.BytesIO(image_bytes), filename="image.jpg"),
            )
            file_id = temp_msg.photo[-1].file_id
        except Exception as upload_err:
            logging.error("Inline image: admin-chat upload failed: %s", upload_err)

        # Delete temp message silently — non-critical
        if temp_msg is not None:
            with contextlib.suppress(Exception):
                await temp_msg.delete()

        if file_id is None:
            logging.error("Inline image: could not obtain file_id, aborting swap")
            with contextlib.suppress(Exception):
                await bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption="❌ Не удалось загрузить изображение. Попробуйте снова.",
                )
            await metrics_collector.record_api_call("pollinations_inline", model, user_id=user_id)
            return

        # ── Step 2: Swap placeholder using the minted file_id ─────────────
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaPhoto(
                    media=file_id,
                    caption=caption,
                    parse_mode="HTML",
                ),
                reply_markup=InlineKeyboardMarkup([]),
            )
            logging.info(
                "Inline image: swapped via file_id in %.1fs for prompt %r (model=%s)",
                elapsed,
                prompt[:60],
                model,
            )
        except Exception as edit_err:
            logging.error("Inline image: edit_message_media failed: %s", edit_err)
            with contextlib.suppress(Exception):
                await bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption="❌ Не удалось обновить изображение. Попробуйте снова.",
                )
    else:
        err_msg = getattr(result, "error_message", "unknown") if result else "provider_exception"
        logging.warning("Inline image: generation failed (%s) for prompt %r", err_msg, prompt[:60])
        with contextlib.suppress(Exception):
            await bot.edit_message_caption(
                inline_message_id=inline_message_id,
                caption=(
                    f"❌ <b>Не удалось сгенерировать изображение.</b>\n"
                    f"<code>{_html.escape(err_msg)}</code>\n\n"
                    "Попробуйте другой запрос или модель."
                ),
                parse_mode="HTML",
            )

    await metrics_collector.record_api_call("pollinations_inline", model, user_id=user_id)


# ── Board initialisation (async) ──────────────────────────────────────────────


async def _init_board_async(
    bot,
    inline_message_id: str,
    topic: str,
    creator_id: int,
) -> None:
    """Persist a new board in the DB. Called as background task from ChosenInlineResult.

    We cannot determine chat_id/message_id here — that happens via the first
    callback press (board_link:pending -> board_handler.handle_board_link_callback).
    """
    try:
        from app.repos.boards_repo import create_board

        await create_board(
            inline_msg_id=inline_message_id,
            topic=topic,
            creator_id=creator_id,
        )
        logging.info(
            "Board created: inline_msg_id=%s topic=%r creator=%s",
            inline_message_id,
            topic,
            creator_id,
        )
    except Exception as exc:
        logging.error("Board init failed for inline_msg_id=%s: %s", inline_message_id, exc, exc_info=True)


# ── Fast 3-way Race Requests for inline generation ───────────────────────────


async def _stream_inline_fast(
    preferred_model: str,
    history: list,
    system_instruction: str | None,
    user_id: int | None,
    max_rounds: int = 4,
    enable_web_search: bool = False,
) -> tuple[str | None, list[tuple[str, str]]]:
    """3-way Race Requests accumulator optimised for inline speed.

    Fires 3 keys simultaneously per round. The first to yield a real chunk
    wins; the other two are cancelled instantly. Zero sleep between rounds.
    With 15+ keys and generous gemini-3.1-flash-lite RPD limits, burning
    3 simultaneous slots per round is essentially free operationally.

    Returns:
        (accumulated_text, sources) where sources is a list of (url, title)
        tuples from Grounding metadata. Returns (None, []) when all rounds fail.
    """
    from app.agent_use_cases import AgentRequestUseCase
    from app.providers.base import get_provider_for_model
    from app.providers.gemini import _GroundingMeta
    from app.repos.keys import get_key_status_manager

    use_case = AgentRequestUseCase()
    status_mgr = get_key_status_manager()
    failed_keys: set[str] = set()
    _winner_sources: list[tuple[str, str]] = []  # Grounding citations from winner

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
            return None, []  # No keys available at all

        # Read thinking level dynamically — admin can change via /set_inline_thinking
        # without restarting the container. Falls back to env-var default.
        thinking_level = await get_global_setting("inline_thinking_level", settings.INLINE_THINKING_LEVEL)

        q: asyncio.Queue = asyncio.Queue()

        async def _race(kd: dict, mod: str = resolved_model, _q: asyncio.Queue = q, _tl: str = thinking_level) -> None:  # type: ignore[assignment]
            kh = kd["key_hash"]
            try:
                prov = get_provider_for_model(mod, kd["api_key"])
                async for chunk in prov.stream_response(  # type: ignore[attr-defined]
                    history=history,
                    model_name=mod,
                    system_instruction=system_instruction,
                    thinking_level=_tl,
                    timeout=45.0,
                    enable_web_search=enable_web_search,
                ):
                    # Intercept _GroundingMeta sentinel — don't put in queue as text chunk
                    if isinstance(chunk, _GroundingMeta):
                        await _q.put((kh, chunk, None))
                        continue
                    await _q.put((kh, chunk, None))
            except asyncio.CancelledError:
                pass  # Loser cancelled normally — no sentinel needed
            except Exception as exc:
                await _q.put((kh, None, exc))  # noqa: B023
                return
            await _q.put((kh, _End(kh), None))

        tasks: dict[str, asyncio.Task] = {kd["key_hash"]: asyncio.create_task(_race(kd)) for kd in keys}
        winner_kh: str | None = None
        chunks: list[str] = []
        errors: dict[str, Exception] = {}

        # ── Phase 1: find the first key to yield a real chunk ────────────────
        try:
            while winner_kh is None and len(errors) < len(keys):
                try:
                    kh, chunk, exc = await asyncio.wait_for(q.get(), timeout=50.0)
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
                # Skip _GroundingMeta sentinels — only text triggers winner
                if isinstance(chunk, _GroundingMeta):
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
                    kh, chunk, exc = await asyncio.wait_for(q.get(), timeout=45.0)
                except TimeoutError:
                    logging.warning("Inline: winner drain timed out after 45s")
                    break
                if kh != winner_kh:
                    continue  # Stale item from cancelled loser — discard
                if exc is not None:
                    logging.warning("Inline: winner stream failed mid-flight: %s", exc)
                    break
                if isinstance(chunk, _End):
                    break  # Clean completion
                # Capture grounding sources from winner's _GroundingMeta sentinel
                if isinstance(chunk, _GroundingMeta):
                    _winner_sources = chunk.sources
                    continue
                if chunk:
                    chunks.append(chunk)
        finally:
            for t in tasks.values():
                if not t.done():
                    t.cancel()

        result = "".join(chunks)
        if result.strip() and not is_error_message(result):
            return result, _winner_sources

        # Winner produced error-tagged text — mark all keys failed and retry
        failed_keys.update(kd["key_hash"] for kd in keys)

    return None, []  # All rounds exhausted


# ── Background generation ─────────────────────────────────────────────────────


async def _generate_and_edit_inline(
    bot,
    inline_message_id: str,
    user_query: str,
    tone_id: str,
    user_id: int | None,
) -> None:
    """
    Core async pipeline with progressive UX feedback:

    1. Call ``gemini-2.5-flash-lite`` with **Google Search Grounding** enabled.
       The model searches the web natively for factual/current queries — no
       separate Tavily call needed.  This ensures real-time data (exchange
       rates, weather, news) instead of potentially stale Tavily QnA cache.
       → At 20 s mark: edit placeholder to "⏳ задерживается…" (if still running).
       → Hard timeout at 55 s.
    2. Convert Markdown response to Telegram HTML.
    3. Edit the placeholder inline message in-place.
    """
    from app.prompt_registry import FORMATTING_RULES_COMPACT

    bot_name = bot.first_name or "Bot"
    tone_sys_hint = _tone_hint(tone_id)
    tone_label = _tone_display(tone_id)
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    # ── Check tabs setting early (needed for system prompt) ──────────────────
    tabs_enabled_now = await get_global_setting("inline_tabs_enabled", "off") == "on"

    # ── Step 1: Build system prompt ───────────────────────────────────────────
    # Google Search Grounding (enable_web_search=True) lets the model search
    # the web internally — we just inject today's date so it knows what "now"
    # means, and instruct it to ALWAYS use Google Search for factual queries.
    _tabs_directive = (
        (
            "\n\nВерни ответ строго в формате XML (без пояснений вне тегов):\n"
            "<response>\n"
            "  <tldr>Краткая выжимка в 2-3 предложения</tldr>\n"
            "  <details>Полный развёрнутый ответ</details>\n"
            "  <sources>Список источников, если есть (иначе оставь пустым)</sources>\n"
            "</response>"
        )
        if tabs_enabled_now
        else ""
    )

    system_instruction = (
        f"[system: current_utc_date={today}]\n"
        f"Тон ответа: {tone_sys_hint}\n"
        "Ты — ассистент в инлайн-режиме Telegram. "
        "Пользователь задаёт вопрос прямо из переписки с другим человеком — "
        "отвечай КРАТКО и по существу (не более 3–4 абзацев).\n"
        "Используй инструмент Google Search для каждого фактического запроса "
        "(курсы валют, погода, новости, даты, цены, статистика).\n\n"
        f"{FORMATTING_RULES_COMPACT}\n"
        f"{_tabs_directive}"
    )

    history = [{"role": "user", "parts": [user_query]}]

    # ── Step 2: Generate (3-way Race Requests, up to 4 rounds = 12 key slots) ─
    # For gemini-2.5-flash-lite with 15+ keys at hundreds RPD each,
    # burning 3 simultaneous slots is operationally free and minimises TTFR.
    _gen_start = time.monotonic()
    final_answer: str | None = None
    _grounding_sources: list[tuple[str, str]] = []  # Grounding Citations (url, title)
    _gen_timed_out = False
    _progress_shown = False
    _log_start = api_logger.log_request(
        "gemini_inline",
        model=_INLINE_MODEL,
        query_length=len(user_query),
        tone=tone_id,
    )

    async def _delayed_progress_edit() -> None:
        """At _GEN_PROGRESS_AFTER_S seconds, edit placeholder to show delay notice."""
        nonlocal _progress_shown
        await asyncio.sleep(_GEN_PROGRESS_AFTER_S)
        if _progress_shown:
            return
        _progress_shown = True
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=_progress_delayed_html(bot_name),
                parse_mode="HTML",
                reply_markup=_LOADING_KEYBOARD,
            )

    progress_task = asyncio.create_task(_delayed_progress_edit())

    try:
        final_answer, _grounding_sources = await asyncio.wait_for(
            _stream_inline_fast(
                preferred_model=_INLINE_MODEL,
                history=history,
                system_instruction=system_instruction,
                user_id=user_id,
                max_rounds=4,
                enable_web_search=True,
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
        progress_task.cancel()
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

    # ── Step 3: Format and edit inline message ────────────────────────────────
    # A tagged error response (e.g. quota exhausted) is treated as a failure.
    _is_api_error = bool(final_answer and is_error_message(final_answer))

    if final_answer and final_answer.strip() and not _is_api_error:
        # ── Tabs mode: parse XML segments and show TL;DR with navigation buttons ─
        if tabs_enabled_now:
            segments = _parse_xml_segments(final_answer)
            if segments:  # successful parse — show tabbed UI
                _tabs_store_put(inline_message_id, segments)
                header = f"<b>{_html.escape(tone_label)}</b> · <code>{_html.escape(user_query[:60])}</code>\n\n"
                tldr_body = markdown_to_html(segments["tldr"])
                formatted = header + tldr_body
                if len(formatted) > 4000:
                    formatted = formatted[:3997] + "…"
                # Show tldr first; Details/Sources buttons let user switch view.
                has_sources = bool(segments.get("sources", "").strip())
                btn_row = [InlineKeyboardButton("📑 Подробнее", callback_data=f"inl_tab:details:{inline_message_id}")]
                if has_sources:
                    btn_row.append(
                        InlineKeyboardButton("🔗 Источники", callback_data=f"inl_tab:sources:{inline_message_id}")
                    )
                reply_markup = InlineKeyboardMarkup([btn_row])
                try:
                    await bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=formatted,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                except Exception as edit_err:
                    logging.error("Inline tabs: edit failed: %s", edit_err)
                    with contextlib.suppress(Exception):
                        await bot.edit_message_text(
                            inline_message_id=inline_message_id,
                            text=strip_formatting(formatted)[:4000] or _FALLBACK_ERROR,
                            reply_markup=reply_markup,
                        )
                return  # Done — tabs path handled

        # ── Plain text path (tabs off or XML parse failed) ────────────────────
        header = f"<b>{_html.escape(tone_label)}</b> · <code>{_html.escape(user_query[:60])}</code>\n\n"
        body = markdown_to_html(final_answer.strip())
        formatted = header + body

        # ── Grounding Citations — expandable source list ─────────────────────
        # When Gemini Search Grounding returned real citations, append them as
        # a collapsed blockquote so the user can verify sources without clutter.
        if _grounding_sources:
            from app.utils.ux_improvements import wrap_in_expandable_blockquote

            src_lines = "\n".join(
                f'• <a href="{url}">{_html.escape(title[:70])}</a>'
                for url, title in _grounding_sources[:3]
                if url and title
            )
            if src_lines:
                citations_block = wrap_in_expandable_blockquote(src_lines, label="📎 Источники")
                # Only append if it fits (leave margin for safety)
                if len(formatted) + len(citations_block) + 2 <= 3900:
                    formatted += f"\n\n{citations_block}"

        # Telegram inline messages: hard 4096-char limit.
        if len(formatted) > 4000:
            formatted = formatted[:3997] + "…"
    else:
        from app.errors import strip_error_tag

        if _is_api_error and final_answer:
            formatted = strip_error_tag(final_answer)
        else:
            formatted = _TIMEOUT_ERROR if _gen_timed_out else _GENERATION_ERROR

    # On failure, attach a retry button so the user can re-trigger generation.
    reply_markup_out: InlineKeyboardMarkup | None = None
    is_failure = not (final_answer and final_answer.strip()) or bool(final_answer and is_error_message(final_answer))
    if is_failure:
        retry_id = _store_retry_params(
            user_query=user_query,
            tone_id=tone_id,
            user_id=user_id,
        )
        reply_markup_out = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Повторить", callback_data=f"inl_retry:{retry_id}")]]
        )
    else:
        reply_markup_out = InlineKeyboardMarkup([])  # strip loading indicator

    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=formatted,
            parse_mode="HTML",
            reply_markup=reply_markup_out,
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
                text=plain or _FALLBACK_ERROR,
                reply_markup=reply_markup_out,
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
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                "⏳ Запрос устарел. Пожалуйста, вызовите бот заново.",
            )
        return

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        return

    # Show loading state
    bot_name = context.bot.first_name or "Bot"
    with contextlib.suppress(Exception):
        await query.edit_message_text(
            text=_placeholder_html(bot_name),
            parse_mode="HTML",
            reply_markup=_LOADING_KEYBOARD,
        )

    # Re-run generation as a background task
    from app.utils.background_tasks import get_task_manager

    get_task_manager().submit(
        _generate_and_edit_inline(
            bot=context.bot,
            inline_message_id=inline_message_id,
            user_query=entry["query"],
            tone_id=entry["tone"],
            user_id=entry["user_id"],
        )
    )


# ── XML segment parser (Tabbed Response UI) ───────────────────────────────────


def _parse_xml_segments(text: str) -> dict | None:
    """Extract <tldr>, <details>, <sources> tags from LLM output.

    Returns a dict {tldr, details, sources} on success, or None if the
    expected structure is missing (graceful degradation to plain text).

    Strategy:
      1. Try regex instead of xml.etree to tolerate slightly malformed XML
         and Gemini's occasional markdown wrapping around the response block.
      2. Require at a minimum a non-empty <tldr> block to consider it parseable.
    """
    import re as _re

    def _extract(tag: str) -> str:
        m = _re.search(rf"<{tag}>(.*?)</{tag}>", text, _re.DOTALL | _re.IGNORECASE)
        return m.group(1).strip() if m else ""

    tldr = _extract("tldr")
    if not tldr:
        return None  # Model didn't follow XML format — graceful fallback

    return {
        "tldr": tldr,
        "details": _extract("details"),
        "sources": _extract("sources"),
    }


# ── Tab-switch callback handler ────────────────────────────────────────────────


async def handle_inline_tab_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle tab navigation buttons: 'inl_tab:<segment>:<inline_message_id>'.

    Swaps the inline message content to the requested segment (tldr/details/sources)
    and updates the keyboard to show the back-navigation button.
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    # Pattern: inl_tab:<segment>:<inline_message_id>
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "inl_tab":
        return

    segment_key = parts[1]  # "tldr" | "details" | "sources"
    inline_message_id = parts[2]

    if segment_key not in ("tldr", "details", "sources"):
        return

    segments = _tabs_store_get(inline_message_id)
    if not segments:
        with contextlib.suppress(Exception):
            await query.answer("⏳ Данные устарели. Повторите запрос.", show_alert=True)
        return

    content = segments.get(segment_key, "").strip()
    if not content:
        await query.answer("ℹ️ Этот раздел пуст.", show_alert=True)
        return

    body = markdown_to_html(content)
    if len(body) > 4000:
        body = body[:3997] + "…"

    # Build keyboard: show "back to TL;DR" for detail/source views; show full nav for tldr
    if segment_key == "tldr":
        has_sources = bool(segments.get("sources", "").strip())
        btn_row = [InlineKeyboardButton("📑 Подробнее", callback_data=f"inl_tab:details:{inline_message_id}")]
        if has_sources:
            btn_row.append(InlineKeyboardButton("🔗 Источники", callback_data=f"inl_tab:sources:{inline_message_id}"))
        reply_markup = InlineKeyboardMarkup([btn_row])
    elif segment_key == "details":
        btn_row = [
            InlineKeyboardButton("🔼 TL;DR", callback_data=f"inl_tab:tldr:{inline_message_id}"),
        ]
        if segments.get("sources", "").strip():
            btn_row.append(InlineKeyboardButton("🔗 Источники", callback_data=f"inl_tab:sources:{inline_message_id}"))
        reply_markup = InlineKeyboardMarkup([btn_row])
    else:  # sources
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔼 TL;DR", callback_data=f"inl_tab:tldr:{inline_message_id}")]]
        )

    _LABELS = {"tldr": "TL;DR", "details": "📑 Подробнее", "sources": "🔗 Источники"}
    label = _LABELS.get(segment_key, segment_key)

    try:
        await query.edit_message_text(
            text=f"<b>{label}</b>\n\n{body}",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception as err:
        logging.error("Inline tab switch: edit failed for %s: %s", inline_message_id, err)
