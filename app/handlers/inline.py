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
import os
import re
import time
import uuid
from datetime import UTC, datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputFile,
    InputMediaPhoto,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from app.config import settings
from app.errors import is_error_message
from app.i18n import t
from app.metrics import metrics_collector
from app.repos.settings_repo import get_global_setting
from app.tarot import SpreadType
from app.utils.api_logger import api_logger
from app.utils.text_format import markdown_to_html, strip_formatting


def _get_lang(obj) -> str:
    user_lang = obj.from_user.language_code if obj and obj.from_user else "en"
    return "ru" if user_lang and user_lang.startswith("ru") else "en"

# ── Constants ────────────────────────────────────────────────────────────────

# Primary inline model is now dynamic.
async def get_inline_model() -> str:
    from app.config import settings
    from app.repos.settings_repo import get_global_setting
    default = settings.INLINE_MODEL if settings else "gemini-3.1-flash-lite"
    return await get_global_setting("inline_model", default)

_INLINE_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Outer timeout for the entire generation pipeline.
_GEN_TIMEOUT_S = 55.0
# Seconds after which we edit the placeholder to show a progress message.
_GEN_PROGRESS_AFTER_S = 20.0

# ── Image intent detection ────────────────────────────────────────────────────
# Matches a broad set of image-generation intents in both Russian and English.
# Russian: нарисуй, нарисуй арт/картину, изобрази, сгенерируй (standalone),
#          создай изображение/арт/аватар, сделай картинку, покажи рисунок.
# English: draw, generate image, create image, make an image, imagine, portrait.
_IMAGE_INTENT_RE = re.compile(
    r"(?:"
    # Russian — draw / illustrate
    r"нарисуй|нарисуйте|нарисовать|рисуй"
    r"|изобрази|изобразите|изобразить"
    # generate (standalone — no noun required; prompt follows)
    r"|сгенерируй|сгенерируйте|сгенерировать"
    # create — only when followed by an image noun to avoid false-positives
    # on "создай список", "создай напоминание", etc.
    r"|создай\s+(?:изображение|картинку|рисунок|арт|фото|картину|аватар|постер|обложку|мем)"
    r"|создайте\s+(?:изображение|картинку|рисунок|арт|фото|картину)"
    r"|создать\s+(?:изображение|картинку|рисунок|арт|фото|картину)"
    # make / show
    r"|сделай\s+(?:изображение|картинку|рисунок|арт|фото|картину|аватар|мем)"
    r"|покажи\s+(?:изображение|картинку|рисунок)"
    # English
    r"|draw|draws|drawing"
    r"|generate\s*(?:an?\s+)?(?:image|picture|photo|art|illustration|avatar|meme)"
    r"|create\s*(?:an?\s+)?(?:image|picture|photo|art|illustration|avatar|meme)"
    r"|make\s*(?:an?\s+)?(?:image|picture|photo|art|illustration|avatar|meme)"
    r"|imagine|portrait"
    r")",
    re.IGNORECASE,
)

# ── Smart-routing patterns ────────────────────────────────────────────────────
# Quoted text in prompt → wan-image (renders text accurately on images)
_QUOTED_TEXT_RE = re.compile(r'["\u00ab\u00bb\u201c\u201d].+?["\u00ab\u00bb\u201c\u201d]|\'.+?\'', re.DOTALL)

# Image editing intent → klein (FLUX.2 Klein 4B, edit/inpaint capable).
# INVARIANT: every branch MUST contain an image noun (фото/картинку/изображение/photo/image).
_IMAGE_EDIT_INTENT_RE = re.compile(
    r"(?:"
    # Branch A: Russian standard edit verbs + image noun directly after
    r"(?:измени|отредактируй|добавь|убери|замени|перекраси"
    r"|улучши|подправь|подтяни|ретушируй|обрежь|вырежи|удали)"
    r"\s+(?:фото|фотку|картинку|изображение)"
    r"|"
    # Branch B: Russian background removal — verb + фон/задний план + image noun
    # Natural order: "удали фон с фото", "сотри фон картинки"
    r"(?:удал[ий]|сотри|убери|вырежи)\s+(?:фон|задний\s+план)"
    r"\s+(?:(?:с|на|у|из|со)\s+)?(?:фото|фотки|картинки|картинку|изображени[яе])"
    r"|"
    # Branch C: English standard edit verbs + image noun
    r"(?:edit|change|add|remove|replace|modify|inpaint"
    r"|enhance|retouch|touch.?up|crop|upscale)"
    r"\s+(?:фото|фотку|картинку|изображение|image|photo|picture|the\s+image|the\s+photo)"
    r"|"
    # Branch D: English background removal + required image noun
    r"(?:remove|erase)\s+(?:the\s+)?background"
    r"\s+(?:from\s+)?(?:фото|фотки|картинки|image|photo|picture|the\s+image|the\s+photo)"
    r")",
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
# Format: (result_id, display_title, pollinations_model)
_IMAGE_MODELS_IDS: list[tuple[str, str]] = [
    ("img_turbo", "zimage"),
    ("img_smart", "gptimage"),
    ("img_art", "qwen-image"),
    ("img_meme", "wan-image"),
]

def _get_image_models(lang: str) -> list[tuple[str, str, str]]:
    return [
        ("img_turbo", t("inline.img_turbo", lang), "zimage"),
        ("img_smart", t("inline.img_smart", lang), "gptimage"),
        ("img_art", t("inline.img_art", lang), "qwen-image"),
        ("img_meme", t("inline.img_meme", lang), "wan-image"),
    ]
# Klein is NOT shown in the inline menu — it is auto-routed when user attaches
# an image with an edit intent keyword.
_IMG_KLEIN_ID = "img_edit"
_IMG_KLEIN_MODEL = "klein"

# ── Placeholder images — locally minted file_ids ──────────────────────────────
# Instead of fetching external URLs (which the Local Bot API Server may fail to
# reach), we generate small colored JPEGs via Pillow, upload them once to the
# admin chat to obtain stable Telegram file_ids, then reuse those forever.
# Mapping: result_id → (bg_rgb, fg_rgb, label)
_PLACEHOLDER_STYLES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], str]] = {
    "img_turbo": ((13, 17, 23), (88, 166, 255), "⚡ Turbo"),
    "img_smart": ((13, 43, 13), (0, 230, 118), "🤖 Smart"),
    "img_art": ((26, 13, 46), (204, 119, 255), "🎨 Art"),
    "img_meme": ((43, 21, 0), (255, 170, 0), "🅰️ Meme"),
    _IMG_KLEIN_ID: ((26, 26, 13), (255, 224, 102), "🪄 Edit"),
}

# Lazily populated by _ensure_placeholders() on first inline query.
_placeholder_file_ids: dict[str, str] = {}


# Board intent prefix — compiled once; shared by query and result handlers.
_BOARD_PREFIX_RE = re.compile(r"^(?:доска|board|трекер)\s*:\s*", re.IGNORECASE)

# Crocodile intent prefix.
# Matches: "крокодил:животные", "croc:animals", "крокодил:=пылесос", "croc:=vacuum"
_CROC_PREFIX_RE = re.compile(
    r"^(?:крокодил|крок|croc|crocodile) ?:\s*",
    re.IGNORECASE,
)

# Horoscope intent prefix
_HOROSCOPE_PREFIX_RE = re.compile(
    r"^(?:гороскоп|horoscope)[\s:]*",
    re.IGNORECASE,
)

# Tarot intent prefix
_TAROT_PREFIX_RE = re.compile(
    r"^(?:таро|tarot|расклад)[\s:]*",
    re.IGNORECASE,
)

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


_placeholder_mint_lock = asyncio.Lock()


async def _ensure_placeholders(bot) -> None:
    """Mint file_ids for placeholder images on first inline query.

    Generates small colored JPEGs via Pillow (no network fetch), uploads each
    to the admin chat to capture a stable Telegram ``file_id``, then deletes
    the temporary messages.  Subsequent calls are no-ops.
    """
    if _placeholder_file_ids:
        return

    async with _placeholder_mint_lock:
        # Double-check after acquiring lock (concurrent callers).
        if _placeholder_file_ids:
            return

        from PIL import Image, ImageDraw

        all_ids = [m[0] for m in _IMAGE_MODELS_IDS] + [_IMG_KLEIN_ID]

        for result_id in all_ids:
            style = _PLACEHOLDER_STYLES.get(result_id)
            if not style:
                continue
            bg_rgb, fg_rgb, label = style

            # Generate a large colored JPEG with clearly visible centered text.
            # 800×450 matches 16:9 aspect — looks clean in Telegram's grid.
            W, H = 800, 450
            img = Image.new("RGB", (W, H), bg_rgb)
            draw = ImageDraw.Draw(img)

            # Load the largest available built-in font.
            # load_default(size=N) requires Pillow ≥ 10.1; fall back for older builds.
            from PIL import ImageFont

            try:
                font_large = ImageFont.load_default(size=56)
                font_small = ImageFont.load_default(size=28)
            except TypeError:
                font_large = ImageFont.load_default()
                font_small = font_large

            hint_line = "⏳ Генерация…"

            # Measure and center both lines
            bb1 = draw.textbbox((0, 0), label, font=font_large)
            tw1, th1 = bb1[2] - bb1[0], bb1[3] - bb1[1]
            bb2 = draw.textbbox((0, 0), hint_line, font=font_small)
            tw2, th2 = bb2[2] - bb2[0], bb2[3] - bb2[1]

            gap = 18  # pixels between lines
            total_h = th1 + gap + th2
            y1 = (H - total_h) / 2
            y2 = y1 + th1 + gap

            draw.text(((W - tw1) / 2, y1), label, fill=fg_rgb, font=font_large)
            # Hint line in a slightly dimmer shade
            dim_fg = tuple(max(0, c - 60) for c in fg_rgb)
            draw.text(((W - tw2) / 2, y2), hint_line, fill=dim_fg, font=font_small)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            buf.seek(0)

            try:
                msg = await bot.send_photo(
                    chat_id=settings.ADMIN_ID,
                    photo=InputFile(buf, filename=f"{result_id}.jpg"),
                )
                _placeholder_file_ids[result_id] = msg.photo[-1].file_id
                with contextlib.suppress(Exception):
                    await bot.delete_message(
                        chat_id=settings.ADMIN_ID,
                        message_id=msg.message_id,
                    )
            except Exception:
                logging.exception("Failed to mint placeholder file_id for %s", result_id)

        logging.info(
            "Minted %d/%d inline placeholder file_ids",
            len(_placeholder_file_ids),
            len(all_ids),
        )


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


def _placeholder_html(bot_name: str, lang: str) -> str:
    return t("inline.search_progress", lang, bot_name=_html.escape(bot_name))


def _progress_search_done_html(bot_name: str, lang: str) -> str:
    return t("inline.generate_progress", lang, bot_name=_html.escape(bot_name))


def _progress_delayed_html(bot_name: str, lang: str) -> str:
    return t("inline.delayed", lang, bot_name=_html.escape(bot_name))


def _get_loading_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t("inline.loading", lang), callback_data="inline_noop")]])

# ── Retry store ──────────────────────────────────────────────────────────────
# Keyed by short UUID, stores params needed to re-run _generate_and_edit_inline.
# Entries auto-expire; we prune on every new insert. TTL = 5 minutes.
_RETRY_TTL_S = 300.0
_retry_store: dict[str, dict] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_tones(lang: str) -> list[tuple[str, str, str]]:
    return [
        ("formal", t("inline.tone_formal", lang), t("inline.tone_hint_formal", lang)),
        ("friendly", t("inline.tone_friendly", lang), t("inline.tone_hint_friendly", lang)),
        ("sarcastic", t("inline.tone_sarcastic", lang), t("inline.tone_hint_sarcastic", lang)),
    ]

def _tone_display(tone_id: str, lang: str) -> str:
    for tid, label, _ in _get_tones(lang):
        if tid == tone_id:
            return label
    return tone_id


def _tone_hint(tone_id: str, lang: str) -> str:
    for tid, _, hint in _get_tones(lang):
        if tid == tone_id:
            return hint
    return ""


# ── Public handlers ───────────────────────────────────────────────────────────


def parse_inline_query(query: str) -> dict:
    """Parse an inline query to determine its intent, stripped prompt, and auto-routed models.

    Returns a dict with:
      - is_image_intent: bool
      - stripped_prompt: str
      - has_edit_intent: bool
      - has_quoted_text: bool
    """
    if not query:
        return {}

    user_query = query.strip()
    is_image = bool(_IMAGE_INTENT_RE.search(user_query))
    has_edit_intent = bool(_IMAGE_EDIT_INTENT_RE.search(user_query))

    if has_edit_intent:
        is_image = True

    if not is_image:
        return {
            "is_image_intent": False,
            "stripped_prompt": user_query,
            "has_edit_intent": False,
            "has_quoted_text": False,
        }

    if _IMAGE_INTENT_RE.search(user_query):
        stripped_prompt = _IMAGE_INTENT_RE.sub("", user_query, count=1).strip(" ,-.!") or user_query
    else:
        stripped_prompt = user_query

    has_quoted_text = bool(_QUOTED_TEXT_RE.search(stripped_prompt))

    return {
        "is_image_intent": True,
        "stripped_prompt": stripped_prompt,
        "has_edit_intent": has_edit_intent,
        "has_quoted_text": has_quoted_text,
    }


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return instant placeholder results for any non-empty query.

    Intent routing:
      - Image intent (нарисуй / draw / etc.) → InlineQueryResultCachedPhoto (model variants)
      - Board intent (доска: / board:)        → 1 InlineQueryResultArticle (board template)
      - Default                               → 3 InlineQueryResultArticle (tone variants)
    """
    query = update.inline_query
    if not query:
        return

    logging.info("Inline query from user=%s: %r", query.from_user.id, query.query[:80])

    user_query = query.query.strip()
    lang = _get_lang(query)
    bot_name = context.bot.first_name or "Bot"
    placeholder = _placeholder_html(bot_name, lang)

    if not user_query:
        # Guide the user when no text has been typed yet.
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    id="hint",
                    title=t("inline.hint_title", lang),
                    description=t("inline.hint_desc", lang),
                    input_message_content=InputTextMessageContent(
                        message_text=placeholder,
                        parse_mode="HTML",
                    ),
                    reply_markup=_get_loading_keyboard(lang),
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    # ── Image intent ──────────────────────────────────────────────────────────
    parsed = parse_inline_query(user_query)

    # ── Image intent ──────────────────────────────────────────────────────────
    if parsed.get("is_image_intent"):
        stripped_prompt = parsed["stripped_prompt"]
        has_quoted_text = parsed["has_quoted_text"]
        has_edit_intent = parsed["has_edit_intent"]

        # ── Smart auto-routing ────────────────────────────────────────────────
        if has_edit_intent:
            # Edit/inpaint mode: show only klein
            routed_models: list[tuple[str, str, str]] = [(_IMG_KLEIN_ID, t("inline.img_edit", lang), _IMG_KLEIN_MODEL)]
            auto_hint = t("inline.img_edit_hint", lang)
        elif has_quoted_text:
            # Meme/text mode: wan-image first for text accuracy
            models_for_lang = _get_image_models(lang)
            meme_entry = next((m for m in models_for_lang if m[0] == "img_meme"), None)
            rest = [m for m in models_for_lang if m[0] != "img_meme"]
            routed_models = ([meme_entry] if meme_entry else []) + rest
            auto_hint = t("inline.img_meme_hint", lang)
        else:
            routed_models = _get_image_models(lang)
            auto_hint = ""

        # Ensure placeholder file_ids are minted (lazy, once per process)
        await _ensure_placeholders(context.bot)

        results_img = [
            InlineQueryResultCachedPhoto(
                id=result_id,
                photo_file_id=_placeholder_file_ids.get(result_id, ""),
                title=title,
                description=(f"{auto_hint} · {stripped_prompt[:70]}" if auto_hint else stripped_prompt[:100]),
                caption=(
                    t("inline.img_caption", lang, prompt=_html.escape(stripped_prompt[:200]))
                    + (f"\n<i>{_html.escape(auto_hint)}</i>" if auto_hint else "")
                    + "\n" + t("inline.loading", lang)
                ),
                parse_mode="HTML",
                reply_markup=_get_loading_keyboard(lang),
            )
            for result_id, title, _ in routed_models
            if result_id in _placeholder_file_ids
        ]
        if not results_img:
            logging.error("No placeholder file_ids minted — cannot serve inline image results")
        await query.answer(results_img, cache_time=0, is_personal=True)
        return

    # ── Board / Topic Aggregator intent ───────────────────────────────────────
    if _BOARD_PREFIX_RE.match(user_query):
        topic = _BOARD_PREFIX_RE.sub("", user_query).strip() or user_query
        board_init_html = t("inline.board_init", lang, topic=_html.escape(topic))
        board_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("inline.board_activated", lang), callback_data="board_link:pending")]]
        )
        results_board = [
            InlineQueryResultArticle(
                id="board",
                title=t("inline.board_topic", lang, topic=topic[:60]),
                description=t("inline.board_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=board_init_html,
                    parse_mode="HTML",
                ),
                reply_markup=board_keyboard,
            )
        ]
        await query.answer(results_board, cache_time=0, is_personal=True)
        return

    # ── Crocodile / Chadrades game intent ────────────────────────────────────────
    if _CROC_PREFIX_RE.match(user_query):
        arg = _CROC_PREFIX_RE.sub("", user_query).strip()
        is_custom = arg.startswith("=")
        if is_custom:
            label = t("inline.croc_custom", lang)
            desc = t("inline.croc_custom_desc", lang)
        else:
            cat = arg or "разное"
            label = t("inline.croc_category", lang, cat=cat[:40])
            desc = t("inline.croc_cat_desc", lang)
        croc_init_html = t("inline.croc_init", lang)
        croc_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("inline.croc_loading", lang), callback_data="inline_noop")]]
        )
        results_croc = [
            InlineQueryResultArticle(
                id="croc",
                title=label,
                description=desc,
                input_message_content=InputTextMessageContent(
                    message_text=croc_init_html,
                    parse_mode="HTML",
                ),
                reply_markup=croc_keyboard,
            )
        ]
        await query.answer(results_croc, cache_time=0, is_personal=True)
        return

    # ── Horoscope intent ─────────────────────────────────────────────────────────
    if _HOROSCOPE_PREFIX_RE.match(user_query):
        arg = _HOROSCOPE_PREFIX_RE.sub("", user_query).strip()
        if not arg:
            arg = "Овен на сегодня"
        horoscope_init_html = t("inline.horoscope_init", lang, arg=arg)
        horoscope_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("inline.horoscope_btn", lang), callback_data="inline_noop")]]
        )
        results_horo = [
            InlineQueryResultArticle(
                id="horoscope",
                title=f"✨ Гороскоп: {arg[:40]}",
                description=t("inline.horoscope_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=horoscope_init_html,
                    parse_mode="HTML",
                ),
                reply_markup=horoscope_keyboard,
            )
        ]
        await query.answer(results_horo, cache_time=0, is_personal=True)
        return

    # ── Tarot intent ────────────────────────────────────────────────────────────────────────────
    if _TAROT_PREFIX_RE.match(user_query):
        # 1. Parse intent
        arg = _TAROT_PREFIX_RE.sub("", user_query).strip()
        has_question = bool(arg)  # evaluate BEFORE applying fallback

        if not arg:
            arg = "Без вопроса"

        loading_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("inline.tarot_btn", lang), callback_data="inline_noop")]]
        )
        results_tarot = []

        # 1. Карта дня — только если значимый вопрос не введён
        if not has_question:
            results_tarot.append(
                InlineQueryResultArticle(
                    id="tarot_daily",
                    title=t("inline.tarot_daily_title", lang),
                    description=t("inline.tarot_daily_desc", lang),
                    input_message_content=InputTextMessageContent(
                        message_text=t("inline.tarot_daily_init", lang),
                        parse_mode="HTML",
                    ),
                    reply_markup=loading_keyboard,
                )
            )

        # 2. Да или Нет — всегда в списке
        results_tarot.append(
            InlineQueryResultArticle(
                id="tarot_yesno",
                title=t("inline.tarot_yesno_title", lang),
                description=arg[:80] if has_question else t("inline.tarot_yesno_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=t("inline.tarot_yesno_init", lang),
                    parse_mode="HTML",
                ),
                reply_markup=loading_keyboard,
            )
        )

        # 3. Отношения
        results_tarot.append(
            InlineQueryResultArticle(
                id="tarot_love",
                title=t("inline.tarot_love_title", lang),
                description=arg[:80] if has_question else t("inline.tarot_love_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=t("inline.tarot_love_init", lang),
                    parse_mode="HTML",
                ),
                reply_markup=loading_keyboard,
            )
        )

        # 4. Классический (Прошлое / Настоящее / Будущее)
        results_tarot.append(
            InlineQueryResultArticle(
                id="tarot",
                title=(
                    f"Таро: {arg[:40]}" if has_question
                    else t("inline.tarot_classic_title", lang)
                ),
                description=t("inline.tarot_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=t("inline.tarot_init", lang),
                    parse_mode="HTML",
                ),
                reply_markup=loading_keyboard,
            )
        )

        # 5. Кельтский крест
        results_tarot.append(
            InlineQueryResultArticle(
                id="tarot_celtic",
                title=t("inline.tarot_celtic_title", lang),
                description=arg[:80] if has_question else t("inline.tarot_celtic_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=t("inline.tarot_celtic_init", lang),
                    parse_mode="HTML",
                ),
                reply_markup=loading_keyboard,
            )
        )

        # 6. Мгновенное предсказание — без LLM, контент уже готов
        results_tarot.append(
            InlineQueryResultArticle(
                id="tarot_fortune",
                title=t("inline.tarot_fortune_title", lang),
                description=t("inline.tarot_fortune_desc", lang),
                input_message_content=InputTextMessageContent(
                    message_text=_build_fortune_cookie_html(),
                    parse_mode="HTML",
                ),
                # No loading keyboard — content is already final
            )
        )

        await query.answer(results_tarot, cache_time=0, is_personal=True)
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
            reply_markup=_get_loading_keyboard(lang),
        )
        for tone_id, label, _ in _get_tones(lang)
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
        lang = _get_lang(chosen)
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=t("inline.empty_query", lang, bot_name=_html.escape(bot_name)),
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
        all_known_models = _IMAGE_MODELS_IDS + [(_IMG_KLEIN_ID, _IMG_KLEIN_MODEL)]
        prov_model = next(
            (entry[1] for entry in all_known_models if entry[0] == result_id),
            "zimage",  # fallback to Турбо
        )
        # Extract clean prompt: remove intent verb prefix
        parsed = parse_inline_query(user_query)
        prompt = parsed.get("stripped_prompt", user_query)
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

    # ── Crocodile game path ───────────────────────────────────────────────────────────
    if result_id == "croc":
        arg = _CROC_PREFIX_RE.sub("", user_query).strip()
        get_task_manager().submit(
            _init_croc_game_async(
                bot=context.bot,
                inline_message_id=inline_message_id,
                arg=arg,
                creator_id=user_id or 0,
            )
        )
        return

    # ── Horoscope path ───────────────────────────────────────────────────────────
    if result_id == "horoscope":
        get_task_manager().submit(
            _generate_horoscope_inline(
                bot=context.bot,
                inline_message_id=inline_message_id,
                user_query=user_query,
                user_id=user_id,
            )
        )
        return

    # ── Tarot paths ────────────────────────────────────────────────────────────────────────────
    if result_id.startswith("tarot"):
        # Fortune cookie: content was baked into InputTextMessageContent at query time.
        # The message is already final — no background generation needed.
        if result_id == "tarot_fortune":
            return

        get_task_manager().submit(
            _generate_tarot_inline(
                bot=context.bot,
                inline_message_id=inline_message_id,
                user_query=user_query,
                user_id=user_id,
                spread_type=result_id,
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
        caption = f"🎨 <b>{_html.escape(prompt[:200])}</b>\n<i>{_get_model_emoji(model)} {model} • {elapsed:.1f}s</i>"

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


async def _init_croc_game_async(
    bot,
    inline_message_id: str,
    arg: str,
    creator_id: int,
) -> None:
    """Create a Crocodile game session in Redis and update the TG inline message.

    ``arg`` is the part after the colon:
      - ``=слово``  → custom word mode  (the loader is the word-giver)
      - everything else → category mode  (bot picks random word)
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from app.config import settings
    from app.games.crocodile import create_game
    from app.games.word_bank import (
        find_word_category,
        pick_random_word_for_topic,
        resolve_topic,
        validate_custom_word,
    )

    try:
        webapp_base = getattr(settings, "WEBAPP_BASE_URL", "").rstrip("/")
        if not webapp_base:
            # Derive from WEBHOOK_URL env var if WEBAPP_BASE_URL not set
            webhook_url = os.environ.get("WEBHOOK_URL", "") or ""
            webapp_base = webhook_url.split("/webhook")[0].rstrip("/")

        # ── Resolve word + mode ───────────────────────────────────────────────
        is_generated = False
        topic_id = ""
        sense_context: str | None = None
        if arg.startswith("="):
            raw_word = arg[1:].strip()
            word = validate_custom_word(raw_word)
            if not word:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text="🐊 <b>Крокодил</b>\n<i>❌ Недопустимое слово. Используй: =слово (2-40 символов)</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([]),
                )
                return
            lang = "ru" if any("\u0400" <= c <= "\u04ff" for c in word) else "en"

            category = "Слово игрока (особое)"
            derived_category = find_word_category(word)
            sense_context = derived_category if derived_category else None
            topic_id = f"custom_word:{uuid.uuid5(uuid.NAMESPACE_DNS, word).hex[:16]}"

        else:
            category_raw = arg or "разное"
            try:
                topic = resolve_topic(category_raw)
                redis_used_key = f"croc:used:{creator_id}:{topic.topic_id}"
                word, lang, category, is_generated = await pick_random_word_for_topic(
                    topic,
                    redis_used_key=redis_used_key,
                )
                topic_id = topic.topic_id
                sense_context = topic.category
            except Exception as _croc_exc:
                from app.errors import ProviderOverloadError

                if isinstance(_croc_exc, ProviderOverloadError):
                    # Infrastructure overload — not the user's fault
                    await bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=(
                            "🐊 <b>Крокодил</b>\n"
                            "⏳ Серверы ИИ временно перегружены.\n"
                            "Попробуй ещё раз через пару секунд."
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([]),
                    )
                elif isinstance(_croc_exc, ValueError):
                    # Genuinely unintelligible category
                    await bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=(
                            "🐊 <b>Крокодил</b>\n"
                            f"❌ Не могу понять тему <i>{category_raw}</i>. "
                            "попробуй снова или укажи другую тему."
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([]),
                    )
                else:
                    raise _croc_exc
                return

        # ── Create game session ───────────────────────────────────────────────
        game = await create_game(
            target_word=word,
            category=category,
            lang=lang,
            inline_message_id=inline_message_id,
            creator_id=creator_id,
            topic_id=topic_id,
            sense_context=sense_context,
        )

        # ── Build WebApp URL for the guesser ─────────────────────────────────
        # Prefer t.me deep link (no "Open this link?" dialog) when a Mini App
        # short name is registered with @BotFather.  Falls back to direct URL.
        miniapp_short = getattr(settings, "MINIAPP_SHORT_NAME", "")
        bot_username = getattr(bot, "username", "") or ""
        if miniapp_short and bot_username:
            game_url = f"https://t.me/{bot_username}/{miniapp_short}?startapp={game.game_id}"
        else:
            game_url = f"{webapp_base}/webapp/game?game_id={game.game_id}"

        # ── Edit inline message ───────────────────────────────────────────────
        if arg.startswith("="):
            status_text = (
                "🐊 <b>Крокодил</b>\n"
                "📝 <i>Ты загадал своё слово.</i>\n"
                "Поделись этим сообщением с партнёром — он будет отгадывать!"
            )
        else:
            gen_note = "✨ <i>(тема сгенерирована ИИ)</i>\n" if is_generated else ""
            status_text = (
                f"🐊 <b>Крокодил</b> · <i>{category}</i>\n{gen_note}🎯 Слово загадано! Открой игру и отгадай его."
            )

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Играть", url=game_url)]])
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=status_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logging.info(
            "Croc game created: game_id=%s inline_msg_id=%s category=%s lang=%s topic_id=%s",
            game.game_id,
            inline_message_id,
            category,
            lang,
            topic_id,
        )

    except Exception as exc:
        logging.error(
            "Croc init failed inline_msg_id=%s: %s",
            inline_message_id,
            exc,
            exc_info=True,
        )
        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text="🐊 <b>Крокодил</b>\n<i>❌ Не удалось создать игру. Попробуйте ещё раз.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception:
            pass


# ── Fast 3-way Race Requests for inline generation ───────────────────────────


async def _stream_inline_fast(
    preferred_model: str,
    history: list,
    system_instruction: str | None,
    user_id: int | None,
    max_rounds: int = 4,
    enable_web_search: bool = False,
) -> tuple[str | None, list[tuple[str, str]]]:
    """2+1 Race Requests accumulator optimised for inline speed.

    Fires 2 AI Studio keys + 1 Vertex AI Express slot simultaneously per round.
    The first to yield a real chunk wins; the other two are cancelled instantly.
    Zero sleep between rounds.

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
        # ── Resolve up to 2 distinct AI Studio keys for this round ─────────────
        # (Vertex AI Express adds a 3rd parallel racer — total concurrency = 3)
        keys: list[dict] = []
        resolved_model: str | None = None
        # AI Studio keys race as fallback alongside the primary Vertex slot.
        # Use _INLINE_FALLBACK_MODEL (gemini-2.5-flash-lite) for AI Studio racers.
        _ai_studio_model = _INLINE_FALLBACK_MODEL
        for _ in range(2):
            kd, mdl, _ = await use_case.resolve_ai_request(
                _ai_studio_model,
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

        # ── Vertex AI Express slot ─────────────────────────────────────────────
        # gemini-3.1-flash-lite on Vertex supports Search Grounding and
        # races alongside the 3 AI Studio keys. Uses a pseudo-key-hash so the
        # shared queue logic treats it uniformly.
        _VERTEX_KH = "__vertex_ai__"
        _INLINE_VERTEX_MODEL = "gemini-3.1-flash-lite"
        _vertex_grounding_holder: list[list[tuple[str, str]]] = [[]]  # mutable closure slot

        async def _vertex_race(_q: asyncio.Queue = q) -> None:
            from google.genai import types as _gtypes

            from app.providers.gemini import _GroundingMeta as _GMeta
            from app.providers.gemini import get_vertex_client

            vertex_client = get_vertex_client()
            if vertex_client is None:
                return  # Vertex not configured — skip silently (no sentinel → race ignores slot)
            try:
                _search_tool = _gtypes.Tool(google_search=_gtypes.GoogleSearch())
                _vcfg = _gtypes.GenerateContentConfig(
                    tools=[_search_tool],
                    system_instruction=system_instruction,
                    temperature=0.7,
                )
                # Build Vertex-compatible contents from history
                _vcontents = [
                    _gtypes.Content(
                        role=h.get("role", "user"),
                        parts=[_gtypes.Part(text=str(p)) for p in (h.get("parts") or []) if p],
                    )
                    for h in history
                ]
                resp = await asyncio.wait_for(
                    vertex_client.aio.models.generate_content(
                        model=_INLINE_VERTEX_MODEL,  # noqa: B023
                        contents=_vcontents,  # type: ignore[arg-type]
                        config=_vcfg,
                    ),
                    timeout=45.0,
                )
                text = getattr(resp, "text", None) or ""
                if not text:
                    await _q.put((_VERTEX_KH, None, RuntimeError("empty vertex response")))  # noqa: B023
                    return
                # Extract grounding sources into holder before putting text chunk
                sources: list[tuple[str, str]] = []
                try:
                    for cand in resp.candidates or []:
                        gm = getattr(cand, "grounding_metadata", None)
                        for gc in getattr(gm, "grounding_chunks", None) or []:
                            web = getattr(gc, "web", None)
                            if web:
                                uri = getattr(web, "uri", "") or ""
                                title = getattr(web, "title", "") or ""
                                if uri:
                                    sources.append((uri, title))
                except Exception:
                    pass
                if sources:
                    _vertex_grounding_holder[0] = sources  # noqa: B023
                    await _q.put((_VERTEX_KH, _GMeta(sources=sources), None))  # noqa: B023
                await _q.put((_VERTEX_KH, text, None))  # noqa: B023
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                await _q.put((_VERTEX_KH, None, exc))  # noqa: B023
                return
            await _q.put((_VERTEX_KH, _End(_VERTEX_KH), None))  # noqa: B023

        tasks: dict[str, asyncio.Task] = {kd["key_hash"]: asyncio.create_task(_race(kd)) for kd in keys}
        # Add Vertex racer only if client is available (checked lazily inside the task)
        _vertex_client_available = True  # Task self-skips if None; count slot regardless
        try:
            from app.providers.gemini import get_vertex_client as _gvc

            _vertex_client_available = _gvc() is not None
        except Exception:
            _vertex_client_available = False
        if _vertex_client_available:
            tasks[_VERTEX_KH] = asyncio.create_task(_vertex_race())
        total_racers = len(tasks)

        winner_kh: str | None = None
        chunks: list[str] = []
        errors: dict[str, Exception] = {}

        # ── Phase 1: find the first key to yield a real chunk ────────────────
        try:
            while winner_kh is None and len(errors) < total_racers:
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

        # Record winner health (non-critical) — skip for Vertex pseudo-key
        if winner_kh != _VERTEX_KH:
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
            # If Vertex won, its grounding was stored in the holder (Phase 1 consumed
            # the sentinel before text); supplement _winner_sources from holder.
            if winner_kh == _VERTEX_KH and _vertex_grounding_holder[0] and not _winner_sources:
                _winner_sources = _vertex_grounding_holder[0]
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
    lang: str = "ru",
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
    from telegram import LinkPreviewOptions

    from app.prompt_registry import FORMATTING_RULES_COMPACT

    bot_name = bot.first_name or "Bot"
    tone_sys_hint = _tone_hint(tone_id, lang)
    tone_label = _tone_display(tone_id, lang)
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

    # ── Step 2: Generate (3-way Race Requests, up to 4 rounds) ──────────────────
    # Primary: Vertex AI Express (gemini-3.1-flash-lite) with Search Grounding.
    # Fallback racers: 2x AI Studio keys (gemini-2.5-flash-lite) per round.
    _gen_start = time.monotonic()
    final_answer: str | None = None
    _grounding_sources: list[tuple[str, str]] = []  # Grounding Citations (url, title)
    _gen_timed_out = False
    _progress_shown = False
    _log_start = api_logger.log_request(
        "gemini_inline",
        model=await get_inline_model(),
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
                text=_progress_delayed_html(bot_name, lang),
                parse_mode="HTML",
                reply_markup=_get_loading_keyboard(lang),
            )

    progress_task = asyncio.create_task(_delayed_progress_edit())

    try:
        final_answer, _grounding_sources = await asyncio.wait_for(
            _stream_inline_fast(
                preferred_model=await get_inline_model(),
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
            model=await get_inline_model(),
            response_length=len(final_answer or ""),
        )

    # Record metrics (we're already in a background task — awaiting is safe)
    await metrics_collector.record_api_call("gemini_inline", await get_inline_model(), user_id=user_id)
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
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                except Exception as edit_err:
                    logging.error("Inline tabs: edit failed: %s", edit_err)
                    with contextlib.suppress(Exception):
                        await bot.edit_message_text(
                            inline_message_id=inline_message_id,
                            text=strip_formatting(formatted)[:4000] or t("inline.fallback_error", lang),
                            reply_markup=reply_markup,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
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
            formatted = t("inline.timeout_error", lang) if _gen_timed_out else t("inline.generation_error", lang)

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
            link_preview_options=LinkPreviewOptions(is_disabled=True),
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
                text=plain or t("inline.fallback_error", lang),
                reply_markup=reply_markup_out,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
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
    now = time.monotonic()
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
    query = update.callback_query
    if not query:
        return

    await query.answer()  # dismiss the spinner immediately

    data = query.data or ""
    if not data.startswith("inl_retry:"):
        return

    retry_id = data.split(":", 1)[1]
    entry = _retry_store.pop(retry_id, None)

    if not entry or (time.monotonic() - entry["ts"] > _RETRY_TTL_S):
        # Expired or unknown — edit with a polite message
        lang = _get_lang(query)
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                t("inline.retry_expired", lang),
            )
        return

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        return

    # Show loading state
    lang = _get_lang(query)
    bot_name = context.bot.first_name or "Bot"
    with contextlib.suppress(Exception):
        await query.edit_message_text(
            text=_placeholder_html(bot_name, lang),
            parse_mode="HTML",
            reply_markup=_get_loading_keyboard(lang),
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
            lang=lang,
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
    def _extract(tag: str) -> str:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)
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

# -- Specialized Inline Generators (Tarot / Horoscope) --------------------------



# ── Specialized Inline Generators (Tarot / Horoscope) ──────────────────────────

async def _generate_horoscope_inline(
    bot,
    inline_message_id: str,
    user_query: str,
    user_id: int | None,
) -> None:
    import contextlib

    from app.intent_router import _handle_horoscope
    from app.utils.text_format import markdown_to_html

    res = await _handle_horoscope(user_query)

    # Try to extract sign from the query for the deep-link payload
    _SIGN_MAP = {
        "овен": "aries", "телец": "taurus", "близнецы": "gemini",
        "рак": "cancer", "лев": "leo", "дева": "virgo",
        "весы": "libra", "скорпион": "scorpio", "стрелец": "sagittarius",
        "козерог": "capricorn", "водолей": "aquarius", "рыбы": "pisces",
        "aries": "aries", "taurus": "taurus", "gemini": "gemini",
        "cancer": "cancer", "leo": "leo", "virgo": "virgo",
        "libra": "libra", "scorpio": "scorpio", "sagittarius": "sagittarius",
        "capricorn": "capricorn", "aquarius": "aquarius", "pisces": "pisces",
    }
    detected_sign = "aries"
    query_lower = user_query.lower()
    for ru_or_en, slug in _SIGN_MAP.items():
        if ru_or_en in query_lower:
            detected_sign = slug
            break

    # Build subscribe deep-link button
    try:
        bot_username = (getattr(bot, "username", None) or "").lstrip("@")
    except Exception:
        bot_username = ""

    subscribe_btn = None
    if bot_username:
        subscribe_url = f"https://t.me/{bot_username}?start=subscribe_horoscope_{detected_sign}"
        subscribe_btn = InlineKeyboardButton("🔔 Подписаться на ежедневный гороскоп", url=subscribe_url)

    reply_markup = (
        InlineKeyboardMarkup([[subscribe_btn]]) if subscribe_btn else InlineKeyboardMarkup([])
    )

    if res and res.text:
        html = markdown_to_html(res.text)
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=html[:4000],
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
    else:
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text="❌ Ошибка генерации гороскопа.",
            )




def _build_fortune_cookie_html() -> str:
    """Build an instant fortune cookie message from fortune_telling_ru in tarot.json.

    Called at answerInlineQuery time — no LLM, no latency.
    Falls back to the English fortune_telling if Russian isn't present yet.
    """
    import html as _html_mod

    from app.tarot import get_fortune_cookie

    result = get_fortune_cookie()
    if not result:
        return "\u26a1 <b>\u041f\u0440\u0435\u0434\u0441\u043a\u0430\u0437\u0430\u043d\u0438\u0435</b>\
\
<i>\u041a\u0430\u0440\u0442\u044b \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b.</i>"

    fortune, card_name = result
    return (
        "\u26a1 <b>\u041f\u0440\u0435\u0434\u0441\u043a\u0430\u0437\u0430\u043d\u0438\u0435</b>\
\
"
        f"<i>\u00ab{_html_mod.escape(fortune)}\u00bb</i>\
\
"
        f"\U0001f3a4 {_html_mod.escape(card_name)}"
    )


async def _generate_tarot_inline(
    bot,
    inline_message_id: str,
    user_query: str,
    user_id: int | None,
    spread_type: str = "tarot",
) -> None:
    """Generate and stream a tarot reading for any spread type.

    Spread-specific prompts are built by _build_tarot_system_prompt().
    spread_type maps directly to SpreadType enum values (e.g. 'tarot_love').

    Uses ProviderRouter.get_response() for sequential key rotation: tries one
    key at a time and rotates on 503/UNAVAILABLE (max 3 retries). QNA_MODEL
    (gemini-2.5-flash) is stable enough that parallel racing would only waste
    daily quota.
    """
    import contextlib
    import logging

    from telegram import InlineKeyboardMarkup

    from app.tarot import SPREAD_BY_ID, SpreadType, get_tarot_context
    from app.utils.text_format import markdown_to_html

    async def _edit_failure(reason: str | None = None) -> None:
        retry_id = _store_retry_params(
            user_query=user_query,
            tone_id="formal",
            user_id=user_id,
        )
        reason_text = reason.strip() if reason else "Модель временно не ответила."
        text = (
            f"❌ Карты молчат: {reason_text}\n\n"
            "Повторите запрос или попробуйте обычный inline-ответ — он пойдёт через другой маршрут и модель."
        )
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=text[:4000],
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄 Повторить обычным ИИ", callback_data=f"inl_retry:{retry_id}")]]
                ),
            )

    # Resolve spread
    spread = SPREAD_BY_ID.get(spread_type, SpreadType.CLASSIC)

    # Extract user question (strip "таро" prefix)
    arg = _TAROT_PREFIX_RE.sub("", user_query).strip()
    if not arg:
        arg = "Общий прогноз"

    tarot_ctx, card_names = get_tarot_context(spread)
    if not tarot_ctx:
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text="❌ База Таро недоступна.",
            )
        return

    daily_card_key: tuple[str, str] | None = None
    if spread == SpreadType.DAILY and card_names:
        from app.tarot_daily import get_prepared_daily_reading, parse_card_label, today_reading_date

        daily_card_key = parse_card_label(card_names[0])
        if daily_card_key:
            card_name, orientation = daily_card_key
            with contextlib.suppress(Exception):
                prepared = await get_prepared_daily_reading(
                    reading_date=today_reading_date(),
                    card_name=card_name,
                    orientation=orientation,
                )
                if prepared:
                    cards_str = " • ".join(card_names)
                    final_text = f"🎤 Карта дня\n_Выпала: {cards_str}_\n\n{prepared.body_markdown}"
                    await bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=markdown_to_html(final_text)[:4000],
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([]),
                    )
                    return

    system_instruction = _build_tarot_system_prompt(spread, tarot_ctx, arg)
    prompt = f"Вопрос к Таро: {arg}"

    # Spread-specific header for the final message
    _HEADER_MAP = {
        SpreadType.CLASSIC: "🔮 Таро",
        SpreadType.DAILY:   "🎤 Карта дня",
        SpreadType.YES_NO:  "🔮 Да или Нет",
        SpreadType.LOVE:    "💞 Отношения",
        SpreadType.CELTIC:  "🌙 Кельтский крест",
    }
    header = _HEADER_MAP.get(spread, "🔮 Таро")

    # ── Sequential key rotation via ProviderRouter.get_response() ──
    # gemini-3.5-flash is stable enough that parallel racing
    # would only waste daily quota. ProviderRouter tries one key at a time,
    # rotating to the next on 503/UNAVAILABLE, with health-aware key selection.
    from app.errors import is_error_message
    from app.providers.router import get_provider_router

    router = get_provider_router()
    try:
        preferred_model = "gemini-3.1-flash-lite" if spread == SpreadType.DAILY else "gemini-3.5-flash"
        result, _tokens = await router.get_response(
            preferred_model=preferred_model,
            history=[{"role": "user", "parts": [prompt]}],
            system_instruction=system_instruction,
            user_id=user_id,
            use_openrouter=False,
            max_key_retries=3,
        )
    except Exception as exc:
        logging.error(
            "Tarot generation exception (spread=%s): %s",
            spread_type,
            exc,
            exc_info=True,
        )
        await _edit_failure("ошибка генерации. Попробуйте ещё раз.")
        return

    if not result or not result.strip() or is_error_message(result):
        logging.error(
            "Tarot generation failed (spread=%s): %s",
            spread_type,
            (result or "")[:120],
        )
        if result and is_error_message(result):
            from app.errors import strip_error_tag

            await _edit_failure(strip_error_tag(result))
        else:
            await _edit_failure("пустой ответ от модели.")
        return

    # Defense-in-depth: strip any surrogate codepoints the LLM may emit
    result = result.strip().encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")

    if spread == SpreadType.DAILY and daily_card_key:
        from app.tarot_daily import TAROT_DAILY_MODEL, today_reading_date, upsert_prepared_daily_reading

        card_name, orientation = daily_card_key
        with contextlib.suppress(Exception):
            await upsert_prepared_daily_reading(
                reading_date=today_reading_date(),
                card_name=card_name,
                orientation=orientation,
                language="ru",
                body_markdown=result,
                model_name=TAROT_DAILY_MODEL,
            )

    cards_str = " • ".join(card_names)
    # Only show card list for spreads with a meaningful question
    if spread == SpreadType.DAILY or spread == SpreadType.YES_NO:
        final_text = f"{header}\n_Выпала: {cards_str}_\n\n{result}"
    else:
        final_text = f"{header}: **{arg}**\n_Выпали: {cards_str}_\n\n{result}"
    html = markdown_to_html(final_text)

    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=html[:4000],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([]),
        )
    except Exception as edit_e:
        logging.error(
            "Failed to edit inline message for tarot (HTML parse error?): %s\nHTML:\n%s",
            edit_e,
            html[:500],
        )
        # Last resort: strip HTML and retry as plain text
        with contextlib.suppress(Exception):
            from app.utils.text_format import strip_formatting
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=strip_formatting(html)[:4000] or "❌ Ошибка форматирования.",
                reply_markup=InlineKeyboardMarkup([]),
            )



def _build_tarot_system_prompt(
    spread: "SpreadType",
    tarot_ctx: str,
    arg: str,
) -> str:
    """Return a spread-specific system instruction for the LLM."""
    from app.tarot import SpreadType

    if spread == SpreadType.CLASSIC:
        return (
            "Ты — мистический и мудрый таролог.\n"
            "Твоя задача — сделать расклад Таро на 3 карты для пользователя.\n"
            "ОБЯЗАТЕЛЬНО используй значения выпавших карт (предоставлены ниже), "
            "чтобы дать связный, глубокий и полезный ответ на вопрос/ситуацию пользователя.\n"
            "Не просто перечисляй значения карт, а свяжи их воедино, создав красивую историю "
            "(Прошлое, Настоящее, Будущее).\n"
            f"---\nВЫПАВШИЕ КАРТЫ:\n{tarot_ctx}\n---\n"
            "Ответ должен быть в формате Markdown. Используй мистические эмодзи."
        )

    if spread == SpreadType.DAILY:
        return (
            "Ты — мистический таролог.\n"
            "Пользователь вытянул ОДНУ карту дня — это совет и энергия на сегодня.\n"
            "Используй значение карты (ниже), чтобы дать:\n"
            "1. Краткое описание энергии дня (2–3 предложения)\n"
            "2. Практический совет на сегодня (1–2 предложения)\n"
            "3. От чего стоит остеречься (1 предложение)\n\n"
            "Ответ должен быть КОРОТКИМ (6–8 предложений). "
            "Формат: Markdown. Используй мистические эмодзи.\n"
            f"---\nКАРТА ДНЯ:\n{tarot_ctx}\n---"
        )

    if spread == SpreadType.YES_NO:
        # Determine upright/reversed from the single card's orientation in tarot_ctx
        is_upright = "Прямая" in tarot_ctx
        verdict = "ДА" if is_upright else "НЕТ"
        verdict_context = "ПРЯМО (ответ: ДА)" if is_upright else "ПЕРЕВЁРНУТО (ответ: НЕТ)"
        return (
            "Ты — мистический оракул Таро.\n"
            f"Пользователь задал вопрос формата Да/Нет. Карта выпала {verdict_context}.\n\n"
            "Твоя задача:\n"
            f"1. Чётко объявить вердикт: **{verdict}**\n"
            "2. Обосновать ответ через значение карты (2–3 предложения)\n"
            "3. Краткое напутствие (1 предложение)\n\n"
            "Ответ КОРОТКИЙ (5–6 предложений). "
            "Формат: Markdown. Используй мистические эмодзи.\n"
            f"---\nВЫПАВШАЯ КАРТА:\n{tarot_ctx}\n---"
        )

    if spread == SpreadType.LOVE:
        return (
            "Ты — мистический таролог, специализирующийся на отношениях.\n"
            "Выполни расклад на ОТНОШЕНИЯ из 5 карт в следующих позициях:\n"
            "• Ты — что ты привносишь в отношения\n"
            "• Партнёр — что привносит второй человек\n"
            "• Что вас связывает — основа и сила вашего союза\n"
            "• Что мешает — скрытые препятствия и конфликты\n"
            "• Куда ведёт — вероятный путь развития\n\n"
            "ОБЯЗАТЕЛЬНО используй значения выпавших карт.\n"
            "Создай СВЯЗНУЮ историю об этих отношениях, не просто перечисление.\n"
            "Ответ средней длины (10–15 предложений). "
            "Формат: Markdown. Используй романтические и мистические эмодзи.\n"
            f"---\nВЫПАВШИЕ КАРТЫ:\n{tarot_ctx}\n---"
        )

    if spread == SpreadType.CELTIC:
        return (
            "Ты — мудрый и опытный таролог.\n"
            "Выполни расклад «Кельтский крест» (адаптированный) из 6 карт:\n"
            "• Ситуация — центральная тема, суть вопроса\n"
            "• Препятствие — что перекрывает путь прямо сейчас\n"
            "• Подсознание — глубинные мотивы, скрытые от самого человека\n"
            "• Прошлое — события и энергии, приведшие к текущей ситуации\n"
            "• Ближайшее будущее — что развернётся в ближайшее время\n"
            "• Итог — финальный результат, если текущий курс не изменится\n\n"
            "ОБЯЗАТЕЛЬНО используй значения выпавших карт.\n"
            "Построй ГЛУБОКИЙ и связный нарратив. Это самый подробный расклад.\n"
            "Ответ развёрнутый (15–20 предложений), но ОБЯЗАТЕЛЬНО уложись в 3500 символов.\n"
            "Формат: Markdown. Используй мистические эмодзи.\n"
            f"---\nВЫПАВШИЕ КАРТЫ:\n{tarot_ctx}\n---"
        )

    # Fallback: CLASSIC
    return (
        "Ты — мистический таролог.\n"
        f"---\nВЫПАВШИЕ КАРТЫ:\n{tarot_ctx}\n---\n"
        "Ответь связно и глубоко. Формат: Markdown. Используй мистические эмодзи."
    )
