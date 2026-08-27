# app/i18n.py
"""Internationalization module — string registry with content-based language detection.

Language detection:
    Uses transcript/message text content to detect language (Cyrillic → ru, else en).
    Does NOT use Telegram `language_code` — many Russian speakers use English Telegram.

Usage::

    from app.i18n import t, detect_language

    lang = detect_language(user_message_text)
    await message.reply_text(t("voice.processing", lang))
"""

import re

# Supported languages
SUPPORTED_LANGS = ("ru", "en")
DEFAULT_LANG = "ru"

# ── Language Detection ───────────────────────────────────────────────────────

# Cyrillic Unicode range: basic + supplement
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def detect_language(text: str | None) -> str:
    """Detect language from text content using Cyrillic character density.

    Returns "ru" if ≥20% of alphabetic chars are Cyrillic, else "en".
    Defaults to "ru" if text is empty.

    ⚡ Perf: single-pass loop counts both cyrillic and alpha chars simultaneously,
    avoiding the two-pass approach (regex findall + generator sum) and the
    intermediate list allocation from findall.
    """
    if not text or len(text.strip()) < 3:
        return DEFAULT_LANG

    # Single pass: count cyrillic and total alphabetic chars together.
    # Avoids: (1) _CYRILLIC_RE.findall() which allocates a list of N strings,
    #         (2) a second full iteration via sum(1 for ch in text if ch.isalpha()).
    cyrillic_count = 0
    alpha_count = 0
    for ch in text[:500]:
        if ch.isalpha():
            alpha_count += 1
            # Cyrillic Basic (U+0400–U+04FF) covers Russian, Ukrainian, etc.
            if "\u0400" <= ch <= "\u04ff":
                cyrillic_count += 1

    if alpha_count == 0:
        return DEFAULT_LANG

    return "ru" if (cyrillic_count / alpha_count) >= 0.2 else "en"


# ── String Registry ──────────────────────────────────────────────────────────

_STRINGS: dict[str, dict[str, str]] = {
    # ── Mini App ─────────────────────────────────────────────────────────
    "miniapp.voice.aoede": {"ru": "Нейтральный и естественный", "en": "Neutral and natural"},
    "miniapp.voice.kore": {"ru": "Более энергичный и уверенный", "en": "More energetic and confident"},
    "miniapp.voice.leda": {"ru": "Лёгкий и молодой", "en": "Light and youthful"},
    "miniapp.voice.zephyr": {"ru": "Чёткий и бодрый", "en": "Clear and brisk"},
    "miniapp.voice.charon": {"ru": "Сдержанный и профессиональный", "en": "Restrained and professional"},
    "miniapp.voice.orus": {"ru": "Более глубокий и авторитетный", "en": "Deeper and authoritative"},
    "miniapp.voice.puck": {"ru": "Бодрый", "en": "Upbeat"},
    "miniapp.preset.off_label": {"ru": "Быстрый", "en": "Fast"},
    "miniapp.preset.off_hint": {"ru": "Минимальная задержка, короткие ответы.", "en": "Minimal latency, short answers."},
    "miniapp.preset.low_label": {"ru": "Сбалансированный", "en": "Balanced"},
    "miniapp.preset.low_hint": {"ru": "Лучший режим по умолчанию для live-диалога.", "en": "Best default mode for live dialogue."},
    "miniapp.preset.medium_label": {"ru": "Умный", "en": "Smart"},
    "miniapp.preset.medium_hint": {"ru": "Больше размышления, но выше задержка.", "en": "More thinking, but higher latency."},
    "miniapp.conn.standard_label": {"ru": "Стандартный Live", "en": "Standard Live"},
    "miniapp.conn.standard_summary": {"ru": "без проверки актуальной информации в интернете (просто поболтать)", "en": "without internet fact-checking (just for chatting)"},
    "miniapp.conn.vertex_label": {"ru": "Vertex Live · с доступом в интернет", "en": "Vertex Live · with internet access"},
    "miniapp.conn.vertex_summary": {"ru": "с поиском актуальной информации в интернете (проверка фактов, актуальные новости)", "en": "with live internet search (fact-checking, breaking news)"},
    "miniapp.voice_tag.conversational": {"ru": "Разговорный", "en": "Conversational"},
    "miniapp.voice_tag.calm": {"ru": "Спокойный", "en": "Calm"},
    "miniapp.voice_tag.deep": {"ru": "Глубокий", "en": "Deep"},
    "miniapp.voice_tag.friendly": {"ru": "Дружелюбный", "en": "Friendly"},
    "miniapp.voice_tag.professional": {"ru": "Профессиональный", "en": "Professional"},
    "miniapp.voice_tag.energetic": {"ru": "Энергичный", "en": "Energetic"},
    "miniapp.voice_tag.soft": {"ru": "Мягкий", "en": "Soft"},
    "miniapp.voice_tag.natural_breezy": {"ru": "Естественный/Легкий", "en": "Natural/Breezy"},
    "miniapp.voice_tag.confident_energetic": {"ru": "Уверенный/Энергичный", "en": "Confident/Energetic"},
    "miniapp.voice_tag.upbeat_male": {"ru": "Оживленный мужской", "en": "Upbeat Male"},
    "miniapp.voice_tag.light_youthful": {"ru": "Светлый/Молодой", "en": "Light/Youthful"},
    "miniapp.voice_tag.deep_authoritative": {"ru": "Глубокий/Авторитетный", "en": "Deep/Authoritative"},
    "miniapp.voice_tag.clear_cheerful": {"ru": "Чёткий/Бодрый", "en": "Clear/Cheerful"},
    "miniapp.voice_tag.informative": {"ru": "Информативный", "en": "Informative"},

    # ── Inline Mode ──────────────────────────────────────────────────────
    # Tones
    "inline.tone_formal": {"ru": "🧑‍💼 Формальный ответ", "en": "🧑‍💼 Formal reply"},
    "inline.tone_friendly": {"ru": "😊 Дружеский ответ", "en": "😊 Friendly reply"},
    "inline.tone_sarcastic": {"ru": "😏 Саркастичный ответ", "en": "😏 Sarcastic reply"},
    "inline.tone_hint_formal": {
        "ru": "Отвечай строго, профессионально и по делу. Только факты, без юмора.",
        "en": "Reply strictly, professionally and to the point. Facts only, no humor.",
    },
    "inline.tone_hint_friendly": {
        "ru": "Отвечай тепло, понятно и неформально, как близкий друг. Допускай эмодзи.",
        "en": "Reply warmly, clearly and informally, like a close friend. Emojis OK.",
    },
    "inline.tone_hint_sarcastic": {
        "ru": "Отвечай с приятной иронией и лёгким сарказмом, оставаясь при этом полезным.",
        "en": "Reply with pleasant irony and light sarcasm while remaining helpful.",
    },
    # Loading / progress
    "inline.loading": {"ru": "⏳ Генерация…", "en": "⏳ Generating…"},
    "inline.search_progress": {
        "ru": "🔎 <b>{bot_name}</b> ищет в интернете…",
        "en": "🔎 <b>{bot_name}</b> is searching the web…",
    },
    "inline.generate_progress": {
        "ru": "🧠 <b>{bot_name}</b> собрал информацию, теперь генерирует ответ…",
        "en": "🧠 <b>{bot_name}</b> gathered info, now generating answer…",
    },
    "inline.delayed": {
        "ru": "⏳ <b>{bot_name}</b> задерживается…",
        "en": "⏳ <b>{bot_name}</b> is taking longer than usual…",
    },
    # Empty query hint
    "inline.hint_title": {"ru": "💬 Введите запрос после @бота…", "en": "💬 Type a query after @bot…"},
    "inline.hint_desc": {"ru": "Например: какая погода в Москве?", "en": "Example: what's the weather in London?"},
    "inline.empty_query": {
        "ru": "💬 Чтобы использовать <b>{bot_name}</b>, введите запрос после @бота.",
        "en": "💬 To use <b>{bot_name}</b>, type a query after @bot.",
    },
    # Image models
    "inline.img_turbo": {"ru": "⚡ Турбо", "en": "⚡ Turbo"},
    "inline.img_smart": {"ru": "🧠 Умный", "en": "🧠 Smart"},
    "inline.img_art": {"ru": "🎨 Арт", "en": "🎨 Art"},
    "inline.img_meme": {"ru": "🅰️ Мем", "en": "🅰️ Meme"},
    "inline.img_edit": {"ru": "🪄 Изменить фото", "en": "🪄 Edit photo"},
    "inline.img_edit_hint": {"ru": "✏️ Режим редактирования (Klein)", "en": "✏️ Edit mode (Klein)"},
    "inline.img_meme_hint": {"ru": "🅰️ Обнаружен текст → авто-выбран Мем-режим", "en": "🅰️ Text detected → Meme mode auto-selected"},
    "inline.img_caption": {"ru": "🎨 <b>Запрос:</b> {prompt}", "en": "🎨 <b>Prompt:</b> {prompt}"},
    # Board
    "inline.board_init": {
        "ru": "📋 <b>{topic}</b>\n━━━━━━━━━━━━━━━━━━━━━\n<i>Отвечайте (reply) на это сообщение, чтобы добавить свои идеи.</i>\n\nПока ничего не предложено.",
        "en": "📋 <b>{topic}</b>\n━━━━━━━━━━━━━━━━━━━━━\n<i>Reply to this message to add your ideas.</i>\n\nNothing proposed yet.",
    },
    "inline.board_activated": {"ru": "📋 Доска активирована", "en": "📋 Board activated"},
    "inline.board_topic": {"ru": "📋 Создать доску: {topic}", "en": "📋 Create board: {topic}"},
    "inline.board_desc": {"ru": "Участники смогут добавлять идеи через reply", "en": "Participants can add ideas via reply"},
    # Crocodile
    "inline.croc_custom": {"ru": "🐊 Крокодил: своё слово", "en": "🐊 Crocodile: custom word"},
    "inline.croc_custom_desc": {"ru": "Задаёшь своё слово — второй игрок будет отгадывать", "en": "You set the word — another player will guess"},
    "inline.croc_category": {"ru": "🐊 Крокодил: {cat}", "en": "🐊 Crocodile: {cat}"},
    "inline.croc_cat_desc": {"ru": "Бот загадает слово из категории — второй игрок отгадывает", "en": "Bot picks a word from the category — another player guesses"},
    "inline.croc_init": {"ru": "🐊 <b>Крокодил</b>\n<i>Игра загружается…</i>", "en": "🐊 <b>Crocodile</b>\n<i>Game loading…</i>"},
    "inline.croc_loading": {"ru": "⏳ Загрузка...", "en": "⏳ Loading..."},
    # Horoscope
    "inline.horoscope_init": {"ru": "✨ <b>Гороскоп: {arg}</b>\n<i>Звёзды сходятся…</i>", "en": "✨ <b>Horoscope: {arg}</b>\n<i>The stars are aligning…</i>"},
    "inline.horoscope_btn": {"ru": "⏳ Анализ...", "en": "⏳ Analyzing..."},
    "inline.horoscope_desc": {"ru": "Астрономически точный прогноз", "en": "Astronomically accurate forecast"},
    # Tarot — classic (legacy, kept for backward compat)
    "inline.tarot_init": {"ru": "🔮 <b>Таро</b>\n<i>Тасуем колоду…</i>", "en": "🔮 <b>Tarot</b>\n<i>Shuffling the deck…</i>"},
    "inline.tarot_btn": {"ru": "⏳ Тасуем...", "en": "⏳ Shuffling..."},
    "inline.tarot_desc": {"ru": "Расклад на 3 карты", "en": "3-card spread"},
    "inline.tarot_classic_title": {"ru": "🔮 Прошлое / Настоящее / Будущее", "en": "🔮 Past / Present / Future"},
    # Tarot — card of the day
    "inline.tarot_daily_title": {"ru": "🎴 Карта дня", "en": "🎴 Card of the Day"},
    "inline.tarot_daily_desc": {"ru": "Одна карта — совет и энергия на сегодня", "en": "One card — advice and energy for today"},
    "inline.tarot_daily_init": {
        "ru": "🎴 <b>Карта дня</b>\n<i>Достаём вашу карту…</i>",
        "en": "🎴 <b>Card of the Day</b>\n<i>Drawing your card…</i>",
    },
    # Tarot — yes/no
    "inline.tarot_yesno_title": {"ru": "🔮 Да или Нет", "en": "🔮 Yes or No"},
    "inline.tarot_yesno_desc": {"ru": "Одна карта отвечает на ваш вопрос", "en": "One card answers your question"},
    "inline.tarot_yesno_init": {
        "ru": "🔮 <b>Да или Нет</b>\n<i>Карта решает…</i>",
        "en": "🔮 <b>Yes or No</b>\n<i>The card decides…</i>",
    },
    # Tarot — relationship
    "inline.tarot_love_title": {"ru": "💞 Отношения", "en": "💞 Relationship"},
    "inline.tarot_love_desc": {"ru": "5 карт: ты, партнёр, связь, препятствие, итог", "en": "5 cards: you, partner, bond, obstacle, outcome"},
    "inline.tarot_love_init": {
        "ru": "💞 <b>Расклад на отношения</b>\n<i>Раскладываем 5 карт…</i>",
        "en": "💞 <b>Relationship Spread</b>\n<i>Laying out 5 cards…</i>",
    },
    # Tarot — celtic cross
    "inline.tarot_celtic_title": {"ru": "🌙 Кельтский крест", "en": "🌙 Celtic Cross"},
    "inline.tarot_celtic_desc": {"ru": "6 карт: глубокий анализ ситуации", "en": "6 cards: deep situation analysis"},
    "inline.tarot_celtic_init": {
        "ru": "🌙 <b>Кельтский крест</b>\n<i>Раскладываем 6 карт…</i>",
        "en": "🌙 <b>Celtic Cross</b>\n<i>Laying out 6 cards…</i>",
    },
    # Tarot — fortune cookie (instant, no LLM)
    "inline.tarot_fortune_title": {"ru": "⚡ Мгновенное предсказание", "en": "⚡ Instant Fortune"},
    "inline.tarot_fortune_desc": {"ru": "Одна фраза от карт — без ожидания", "en": "One fortune phrase — instant, no waiting"},

    # Errors
    "inline.timeout_error": {
        "ru": "⏰ Модель не успела ответить вовремя. Нажмите «Повторить» ниже.",
        "en": "⏰ Model didn't respond in time. Press «Retry» below.",
    },
    "inline.generation_error": {"ru": "❌ Не удалось получить ответ.", "en": "❌ Failed to get a response."},
    "inline.fallback_error": {"ru": "Ошибка генерации ответа.", "en": "Response generation error."},
    "inline.retry_expired": {
        "ru": "⌛ Ссылка на повтор устарела. Введите запрос заново.",
        "en": "⌛ Retry link has expired. Please send the query again.",
    },
    "inline.btn_continue": {
        "ru": "💬 Обсудить в ЛС",
        "en": "💬 Discuss in DM",
    },
    "inline.btn_ask_more": {
        "ru": "🔄 Ещё вопрос",
        "en": "🔄 Ask more",
    },
    "inline.followup_title": {
        "ru": "↪️ Продолжить обсуждение",
        "en": "↪️ Continue discussion",
    },
    "inline.followup_hint": {
        "ru": "Допишите новый вопрос после стрелки",
        "en": "Type the next question after the arrow",
    },
    "inline.followup_expired": {
        "ru": "⏰ Контекст устарел. Задайте новый inline-вопрос.",
        "en": "⏰ Context expired. Start a new inline question.",
    },

    # ── Voice Handler ────────────────────────────────────────────────────
    "voice.processing": {
        "ru": "🎙️ Обрабатываю голосовое сообщение...",
        "en": "🎙️ Processing voice message...",
    },
    "voice.too_short": {
        "ru": "⚠️ Голосовое сообщение слишком короткое.",
        "en": "⚠️ Voice message is too short.",
    },
    "voice.download_failed": {
        "ru": "❌ Не удалось загрузить голосовое сообщение.",
        "en": "❌ Failed to download voice message.",
    },
    "voice.transcription_failed": {
        "ru": "❌ Не удалось расшифровать голосовое сообщение. Попробуйте ещё раз.",
        "en": "❌ Failed to transcribe voice message. Please try again.",
    },
    "voice.error": {
        "ru": "❌ Произошла ошибка при обработке голосового сообщения.",
        "en": "❌ An error occurred while processing the voice message.",
    },
    "voice.transcript_label": {
        "ru": "🎙️ *Расшифровка:*",
        "en": "🎙️ *Transcript:*",
    },
    "voice.history_marker": {
        "ru": "[Голосовое сообщение]",
        "en": "[Voice message]",
    },
    "voice.confirm_prompt": {
        "ru": "Правильно ли я вас понял? Отправляем этот запрос?",
        "en": "Did I understand you correctly? Send this request?",
    },
    "voice.btn_confirm": {
        "ru": "✅ Да, отправить",
        "en": "✅ Yes, send",
    },
    "voice.btn_transcribe_only": {
        "ru": "📝 Только расшифровка",
        "en": "📝 Transcript only",
    },
    "voice.btn_edit": {
        "ru": "✏️ Изменить",
        "en": "✏️ Edit",
    },
    "voice.btn_cancel": {
        "ru": "❌ Отмена",
        "en": "❌ Cancel",
    },
    "voice.cancelled": {
        "ru": "❌ Отменено.",
        "en": "❌ Cancelled.",
    },
    "voice.sending_request": {
        "ru": "🤔 Обрабатываю ваш запрос...",
        "en": "🤔 Processing your request...",
    },
    "voice.edit_prompt": {
        "ru": "✏️ Отправьте исправленный текст:",
        "en": "✏️ Send the corrected text:",
    },
    "voice.edit_original": {
        "ru": "📝 Исходная расшифровка:",
        "en": "📝 Original transcript:",
    },
    "voice.no_pending": {
        "ru": "❌ Нет ожидающего голосового запроса.",
        "en": "❌ No pending voice request.",
    },
    "voice.btn_deep_search": {
        "ru": "🔍 Глубокий поиск (Agent)",
        "en": "🔍 Deep Search (Agent)",
    },
    "voice.auto_confirm": {
        "ru": "Автоматически отправлено (простой запрос)",
        "en": "Auto-sent (simple request)",
    },
    "voice.show_tell_detected": {
        "ru": "📸 К запросу прикреплено изображение из ответа",
        "en": "📸 Image from replied message attached",
    },
    "voice.deep_search_starting": {
        "ru": "🔍 Запускаю глубокий поиск...",
        "en": "🔍 Starting deep search...",
    },
    # ── General Errors ───────────────────────────────────────────────────
    "error.generic": {
        "ru": "❌ Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
        "en": "❌ An error occurred while processing your request. Please try again.",
    },
    "error.command": {
        "ru": "❌ Произошла ошибка при обработке команды. Попробуйте позже.",
        "en": "❌ An error occurred while processing the command. Please try later.",
    },
    "error.rate_limit": {
        "ru": "⏱️ Превышен лимит запросов. Пожалуйста, подождите немного перед следующим запросом.",
        "en": "⏱️ Rate limit exceeded. Please wait a moment before your next request.",
    },
    "error.message_too_long": {
        "ru": "❌ Сообщение слишком длинное. Максимум 4096 символов.\nСократите текст и отправьте снова.",
        "en": "❌ Message is too long. Maximum 4096 characters.\nShorten the text and try again.",
    },
    "error.no_api_keys": {
        "ru": "❌ Сервис ответов временно недоступен. Пожалуйста, попробуйте позже.",
        "en": "❌ The response service is temporarily unavailable. Please try again later.",
    },
    "error.retry_failed": {
        "ru": "❌ Произошла ошибка при повторе запроса.",
        "en": "❌ An error occurred while retrying the request.",
    },
    "error.no_retry_data": {
        "ru": "❌ Нет запроса для повтора.",
        "en": "❌ No request to retry.",
    },
    "error.original_not_found": {
        "ru": "❌ Не удалось найти оригинальное сообщение.",
        "en": "❌ Could not find the original message.",
    },
    "error.empty_response": {
        "ru": "Получен пустой ответ от API.",
        "en": "Received an empty response from the API.",
    },
    "error.all_exhausted": {
        "ru": "🚫 Все лимиты для всех моделей на сегодня исчерпаны. Попробуйте позже.",
        "en": "🚫 All model limits have been exhausted for today. Please try later.",
    },
    # ── Busy/Wait ────────────────────────────────────────────────────────
    "busy.toast": {
        "ru": "⏳ Дождитесь завершения текущего запроса",
        "en": "⏳ Please wait for the current request to finish",
    },
    # ── Processing Indicators ────────────────────────────────────────────
    "processing.thinking": {
        "ru": "🤔 Думаю...",
        "en": "🤔 Thinking...",
    },
    "processing.image": {
        "ru": "🖼️ Обрабатываю изображение...",
        "en": "🖼️ Processing image...",
    },
    "processing.simplified": {
        "ru": "🤔 Обрабатываю ваш запрос... (упрощенный режим)",
        "en": "🤔 Processing your request... (simplified mode)",
    },
    "processing.retry": {
        "ru": "🔁 Повторяю предыдущий запрос…",
        "en": "🔁 Retrying previous request…",
    },
    "processing.describing_image": {
        "ru": "🖼️ Описываю изображение...",
        "en": "🖼️ Describing image...",
    },
    # ── Navigation / Menus ───────────────────────────────────────────────
    "menu.new_chat": {
        "ru": "💬 Новый чат",
        "en": "💬 New chat",
    },
    "menu.model": {
        "ru": "🧠 Модель AI",
        "en": "🧠 AI Model",
    },
    "menu.roles": {
        "ru": "🎭 Роли",
        "en": "🎭 Roles",
    },
    "menu.documents": {
        "ru": "📄 Документы",
        "en": "📄 Documents",
    },
    "menu.conversations": {
        "ru": "💬 Беседы",
        "en": "💬 Conversations",
    },
    "menu.search_toggle": {
        "ru": "🌐 Поиск",
        "en": "🌐 Search",
    },
    "menu.help": {
        "ru": "❓ Помощь",
        "en": "❓ Help",
    },
    "menu.back": {
        "ru": "⬅️ Назад",
        "en": "⬅️ Back",
    },
    "menu.back_to_menu": {
        "ru": "⬅️ Меню",
        "en": "⬅️ Menu",
    },
    # ── New Chat / Topic ─────────────────────────────────────────────────
    "chat.new_topic": {
        "ru": "✅ Новый чат создан. История и системная инструкция сброшены.",
        "en": "✅ New chat created. History and system prompt have been reset.",
    },
    "chat.new_started": {
        "ru": "✨ **Новый чат начат!**\n\nКонтекст и роль сброшены. Напишите что-нибудь. 👇",
        "en": "✨ **New chat started!**\n\nContext and role have been reset. Type something. 👇",
    },
    "chat.new_cleared_toast": {
        "ru": "✨ Чат очищен!",
        "en": "✨ Chat cleared!",
    },
    "chat.start_with_role": {
        "ru": "🎭 Начать с роли",
        "en": "🎭 Start with a role",
    },
    "chat.change_model": {
        "ru": "🧠 Сменить модель",
        "en": "🧠 Change model",
    },
    "chat.returned_to_chat": {
        "ru": "💬 Вы вернулись в обычный чат. История сохранена — можете продолжить общение!",
        "en": "💬 You've returned to regular chat. History saved — you can continue!",
    },
    "chat.research_ended": {
        "ru": "💬 Режим исследования завершён",
        "en": "💬 Research mode ended",
    },
    "chat.deeper_dive": {
        "ru": "Супер! Мы готовы *копнуть глубже*! 😉 \nЧто еще вы хотели бы узнать по этой теме?",
        "en": "Great! We're ready to *dig deeper*! 😉 \nWhat else would you like to know about this topic?",
    },
    # ── Buttons ──────────────────────────────────────────────────────────
    "btn.retry": {
        "ru": "🔄 Попробовать ещё раз",
        "en": "🔄 Try again",
    },
    "btn.roles": {
        "ru": "🎭 Роль ИИ",
        "en": "🎭 AI Role",
    },
    "btn.what_if": {
        "ru": "🔀 Что если…",
        "en": "🔀 What if…",
    },
    "btn.back_to_main": {
        "ru": "↩️ К основной ветке",
        "en": "↩️ Back to main branch",
    },
    "btn.new_topic": {
        "ru": "✨ Начать новую тему",
        "en": "✨ Start new topic",
    },
    "btn.cancel": {
        "ru": "❌ Отмена",
        "en": "❌ Cancel",
    },
    "btn.operation_cancelled": {
        "ru": "Операция отменена.",
        "en": "Operation cancelled.",
    },
    # ── Public help and Telegram command menu ────────────────────────────
    "help.overview.title": {
        "ru": "📚 Чем я могу помочь",
        "en": "📚 How I can help",
    },
    "help.overview.intro": {
        "ru": "Ниже — быстрый обзор возможностей. Выберите тему, чтобы увидеть понятные подсказки.",
        "en": "Here is a quick overview. Choose a topic for clear, practical guidance.",
    },
    "help.overview.footer": {
        "ru": "Не знаете, с чего начать? Просто напишите задачу обычными словами — я подскажу следующий шаг.",
        "en": "Not sure where to begin? Describe your task in your own words and I’ll suggest the next step.",
    },
    "help.category.chat.title": {
        "ru": "💬 Разговор и начало",
        "en": "💬 Chat and getting started",
    },
    "help.category.chat.button": {"ru": "💬 Чат", "en": "💬 Chat"},
    "help.category.personalize.title": {
        "ru": "🎛 Настройка ответов",
        "en": "🎛 Personalize responses",
    },
    "help.category.personalize.button": {"ru": "🎛 Настроить", "en": "🎛 Personalize"},
    "help.category.search.title": {
        "ru": "🌐 Поиск и обзоры",
        "en": "🌐 Search and briefings",
    },
    "help.category.search.button": {"ru": "🌐 Поиск", "en": "🌐 Search"},
    "help.category.create.title": {
        "ru": "🧰 Документы, изображения и голос",
        "en": "🧰 Documents, images, and voice",
    },
    "help.category.create.button": {"ru": "🧰 Создать", "en": "🧰 Create"},
    "help.category.history.title": {
        "ru": "🗂 Беседы и история",
        "en": "🗂 Conversations and history",
    },
    "help.category.history.button": {"ru": "🗂 Беседы", "en": "🗂 Conversations"},
    "help.category.privacy.title": {
        "ru": "🔐 Память и ваши данные",
        "en": "🔐 Memory and your data",
    },
    "help.category.privacy.button": {"ru": "🔐 Данные", "en": "🔐 Your data"},
    "help.category.games.title": {
        "ru": "🎮 Игры и напоминания",
        "en": "🎮 Games and reminders",
    },
    "help.category.games.button": {"ru": "🎮 Игры", "en": "🎮 Games"},
    "help.category.insights.title": {
        "ru": "🔮 Таро, гороскоп и натальная карта",
        "en": "🔮 Tarot, horoscope, and natal chart",
    },
    "help.category.insights.button": {"ru": "🔮 Практики", "en": "🔮 Insights"},
    "help.back_to_help": {
        "ru": "⬅️ К справке",
        "en": "⬅️ Back to help",
    },
    "help.topic_not_found": {
        "ru": "❓ Тема не найдена.",
        "en": "❓ Topic not found.",
    },
    "help.topic.chat": {
        "ru": "Напишите сообщение, отправьте фото или голосовое — я отвечу в том же диалоге. Новый чат начинает тему с чистого контекста, но не удаляет сохранённые данные.",
        "en": "Send a message, photo, or voice note and I’ll reply in the same conversation. A new chat starts with fresh context without deleting saved data.",
    },
    "help.topic.personalize": {
        "ru": "Выберите подходящую модель, роль и глубину ответа. Своя инструкция помогает заранее задать тон, формат или постоянные пожелания.",
        "en": "Choose a model, role, and response depth that suit the task. A custom instruction can set the tone, format, or ongoing preferences.",
    },
    "help.topic.search": {
        "ru": "Поставьте ? перед вопросом для быстрого поиска или ?? для более подробного исследования с источниками. Ежедневный обзор можно включить и отключить отдельными командами.",
        "en": "Put ? before a question for a quick search, or ?? for more thorough research with sources. Daily briefings can be turned on or off separately.",
    },
    "help.topic.create": {
        "ru": "Отправьте документ, чтобы задавать вопросы по его содержимому, опишите желаемое изображение или откройте голосовой разговор. Live Audio показывается, когда он доступен в текущей установке.",
        "en": "Send a document to ask about its contents, describe an image you want to create, or start a voice conversation. Live Audio appears when it is available in this installation.",
    },
    "help.topic.history": {
        "ru": "Сохраняйте важные беседы, возвращайтесь к ним позже, переименовывайте и удаляйте ненужные. Текущий чат можно скачать отдельным файлом.",
        "en": "Save important conversations, return to them later, rename them, or remove those you no longer need. You can also download the current chat as a file.",
    },
    "help.topic.privacy": {
        "ru": "В приватном чате можно увидеть и удалить отдельные воспоминания, очистить всю долгосрочную память, скачать свои данные или навсегда удалить аккаунт. Перед необратимым удалением потребуется явное подтверждение.",
        "en": "In a private chat you can review and remove individual memories, clear long-term memory, download your data, or permanently delete the account. Irreversible deletion always requires explicit confirmation.",
    },
    "help.topic.games": {
        "ru": "Откройте игровой каталог или сразу запустите ежедневную игру. Напоминание поможет не забыть о задаче в нужное время.",
        "en": "Open the game hub or launch a daily game directly. Reminders can bring a task back at the right time.",
    },
    "help.topic.insights": {
        "ru": "Здесь собраны расклады Таро, натальная карта и ежедневный гороскоп. Настройки гороскопа позволяют выбрать удобное время или полностью остановить доставку.",
        "en": "This section includes Tarot readings, a natal chart, and daily horoscopes. Horoscope settings let you choose delivery times or stop delivery completely.",
    },
    "help.command.start": {"ru": "Открыть главное меню", "en": "Open the main menu"},
    "help.command.help": {"ru": "Посмотреть возможности бота", "en": "See what the bot can do"},
    "help.command.newchat": {"ru": "Начать новый диалог", "en": "Start a new conversation"},
    "help.command.model": {"ru": "Выбрать модель для ответов", "en": "Choose a response model"},
    "help.command.roles": {"ru": "Выбрать стиль и специализацию", "en": "Choose a style and specialization"},
    "help.command.setprompt": {"ru": "Задать свою инструкцию боту", "en": "Set a custom instruction"},
    "help.command.thinking": {"ru": "Настроить глубину ответа", "en": "Adjust response depth"},
    "help.command.settings": {"ru": "Открыть все настройки", "en": "Open all settings"},
    "help.command.res": {"ru": "Включить или выключить веб-поиск", "en": "Turn web search on or off"},
    "help.command.subscribe": {"ru": "Получать ежедневный обзор", "en": "Receive a daily briefing"},
    "help.command.unsubscribe": {"ru": "Отключить ежедневный обзор", "en": "Stop the daily briefing"},
    "help.command.documents": {"ru": "Открыть мои документы", "en": "Open my documents"},
    "help.command.draw": {"ru": "Создать изображение по описанию", "en": "Create an image from a prompt"},
    "help.command.live": {"ru": "Начать голосовой разговор", "en": "Start a voice conversation"},
    "help.command.save": {"ru": "Сохранить текущую беседу", "en": "Save the current conversation"},
    "help.command.conversations": {"ru": "Показать сохранённые беседы", "en": "Show saved conversations"},
    "help.command.switch": {"ru": "Перейти к другой беседе", "en": "Switch to another conversation"},
    "help.command.rename": {"ru": "Переименовать беседу", "en": "Rename a conversation"},
    "help.command.delete": {"ru": "Удалить сохранённую беседу", "en": "Delete a saved conversation"},
    "help.command.export": {"ru": "Скачать текущий чат", "en": "Download the current chat"},
    "help.command.stats": {"ru": "Посмотреть статистику использования", "en": "View usage statistics"},
    "help.command.memory": {"ru": "Посмотреть сохранённые воспоминания", "en": "Review saved memories"},
    "help.command.clearmemory": {"ru": "Очистить долгосрочную память", "en": "Clear long-term memory"},
    "help.command.mydata": {"ru": "Скачать свои данные", "en": "Download your personal data"},
    "help.command.deleteme": {"ru": "Навсегда удалить аккаунт", "en": "Permanently delete the account"},
    "help.command.games": {"ru": "Открыть каталог мини-игр", "en": "Open the mini-game hub"},
    "help.command.dailycroc": {"ru": "Сыграть в ежедневного Крокодила", "en": "Play Daily Crocodile"},
    "help.command.daily2048": {"ru": "Сыграть в ежедневную 2048", "en": "Play Daily 2048"},
    "help.command.trivia": {"ru": "Ответить на вопросы дня", "en": "Answer today’s trivia"},
    "help.command.remind": {"ru": "Создать напоминание", "en": "Create a reminder"},
    "help.command.tarot": {"ru": "Начать расклад Таро", "en": "Start a Tarot reading"},
    "help.command.natal": {"ru": "Составить натальную карту", "en": "Create a natal chart"},
    "help.command.horoscope_settings": {"ru": "Настроить ежедневный гороскоп", "en": "Set up daily horoscopes"},
    "help.command.horoscope_stop": {"ru": "Остановить доставку гороскопа", "en": "Stop horoscope delivery"},
    # ── Search Toggle ────────────────────────────────────────────────────
    "search.on": {
        "ru": "ВКЛЮЧЕН",
        "en": "ON",
    },
    "search.off": {
        "ru": "ВЫКЛЮЧЕН",
        "en": "OFF",
    },
    # ── Complex Search ───────────────────────────────────────────────────
    "complex.detected": {
        "ru": "Обнаружен сложный запрос (изображение + поиск). Это потребует нескольких шагов и потратит больше времени. Что вы хотите сделать?",
        "en": "Complex request detected (image + search). This will require multiple steps and take longer. What would you like to do?",
    },
    "complex.vision_only": {
        "ru": "🖼️ Только описать фото",
        "en": "🖼️ Describe photo only",
    },
    "complex.confirm": {
        "ru": "🔎 Выполнить сложный поиск",
        "en": "🔎 Perform complex search",
    },
    # ── Documents ────────────────────────────────────────────────────────
    "doc.error_processing": {
        "ru": "❌ Произошла ошибка при обработке документа. Попробуйте другой файл.",
        "en": "❌ An error occurred while processing the document. Please try another file.",
    },
    "doc.error_question": {
        "ru": "❌ Произошла ошибка при обработке вопроса. Попробуйте переформулировать.",
        "en": "❌ An error occurred while processing the question. Try rephrasing.",
    },
    "doc.to_documents": {
        "ru": "📄 К документам",
        "en": "📄 Go to documents",
    },
    "doc.not_found": {
        "ru": "❌ Документ не найден.",
        "en": "❌ Document not found.",
    },
    "doc.no_documents_to_delete": {
        "ru": "У вас нет документов для удаления.",
        "en": "You have no documents to delete.",
    },
    "doc.delete_error": {
        "ru": "❌ Ошибка при удалении документа.",
        "en": "❌ Error deleting document.",
    },
    "doc.main_menu": {
        "ru": "🏠 Главное меню",
        "en": "🏠 Main menu",
    },
    "doc.processing": {
        "ru": "📄 Обрабатываю документ...",
        "en": "📄 Processing document...",
    },
    # ── Image Processing ─────────────────────────────────────────────────
    "image.error": {
        "ru": "❌ Произошла ошибка при обработке изображения.",
        "en": "❌ An error occurred while processing the image.",
    },
    "image.error_retry": {
        "ru": "❌ Произошла ошибка при обработке изображения. Попробуйте ещё раз.",
        "en": "❌ An error occurred while processing the image. Please try again.",
    },
    "image.group_error": {
        "ru": "❌ Произошла ошибка при обработке группы изображений.",
        "en": "❌ An error occurred while processing the image group.",
    },
    "image.group_error_retry": {
        "ru": "❌ Произошла ошибка при обработке группы изображений. Попробуйте ещё раз.",
        "en": "❌ An error occurred while processing the image group. Please try again.",
    },
    "image.media_group_overflow": {
        "ru": "⚠️ Слишком много одновременных медиа-групп. Попробуйте позже.",
        "en": "⚠️ Too many simultaneous media groups. Please try later.",
    },
    # ── LTM / Memory ────────────────────────────────────────────────────
    "ltm.memories_injected": {
        "ru": "\n\n_🧠 Использован контекст из прошлых бесед ({count})_",
        "en": "\n\n_🧠 Context from past conversations used ({count})_",
    },
    # ── Settings ─────────────────────────────────────────────────────────
    "settings.title": {
        "ru": "⚙️ **Настройки**",
        "en": "⚙️ **Settings**",
    },
    "settings.model": {
        "ru": "🧠 **Модель:**",
        "en": "🧠 **Model:**",
    },
    "settings.thinking": {
        "ru": "💡 **Мышление:**",
        "en": "💡 **Thinking:**",
    },
    "settings.search": {
        "ru": "🌐 **Поиск:**",
        "en": "🌐 **Search:**",
    },
    "settings.role_label": {
        "ru": "🎭 **Роль:**",
        "en": "🎭 **Role:**",
    },
    "settings.ltm_label": {
        "ru": "📚 **Долгосрочная память:**",
        "en": "📚 **Long-term memory:**",
    },
    "settings.enabled": {
        "ru": "✅ Включена",
        "en": "✅ Enabled",
    },
    "settings.disabled": {
        "ru": "❌ Выключена",
        "en": "❌ Disabled",
    },
    "settings.default_role": {
        "ru": "(стандартная)",
        "en": "(default)",
    },
    "settings.default_model": {
        "ru": "(по умолчанию)",
        "en": "(default)",
    },
    "settings.btn_change_model": {
        "ru": "🧠 Сменить модель",
        "en": "🧠 Change model",
    },
    "settings.btn_thinking": {
        "ru": "💡 Мышление",
        "en": "💡 Thinking",
    },
    "settings.btn_search": {
        "ru": "🌐 Поиск",
        "en": "🌐 Search",
    },
    "settings.btn_roles": {
        "ru": "🎭 Роли",
        "en": "🎭 Roles",
    },
    "settings.search_enabled": {
        "ru": "✅ Включён",
        "en": "✅ Enabled",
    },
    "settings.search_disabled": {
        "ru": "❌ Выключен",
        "en": "❌ Disabled",
    },
    # ── Dedup ────────────────────────────────────────────────────────────
    "dedup.skipping": {
        "ru": "⏳ Запрос уже обрабатывается…",
        "en": "⏳ Request is already being processed…",
    },
    # ── Fallback Confirmation ────────────────────────────────────────────
    "chat.confirm_fallback": {
        "ru": "Да, использовать {model}",
        "en": "Yes, use {model}",
    },
    "chat.cancel_fallback": {
        "ru": "Нет, отмена",
        "en": "No, cancel",
    },
    "chat.model_thinking": {
        "ru": "🧠 Модель {model} думает...",
        "en": "🧠 Model {model} is thinking...",
    },
    # ── Extra Buttons ────────────────────────────────────────────────────
    "btn.listen": {
        "ru": "🔊 Озвучить",
        "en": "🔊 Listen",
    },
    "btn.new_topic_short": {
        "ru": "✨ Новая тема",
        "en": "✨ New topic",
    },
    "btn.try_model": {
        "ru": "⚡ Попробовать {model}",
        "en": "⚡ Try {model}",
    },
    "btn.continue_stream": {
        "ru": "Продолжить",
        "en": "Continue",
    },
    "processing.continuing": {
        "ru": "▶️ Продолжаю прерванный ответ…",
        "en": "▶️ Continuing interrupted response…",
    },
    # ── Streaming Status ─────────────────────────────────────────────────
    "stream.blocked_by_safety": {
        "ru": "\n\n⚠️ _Ответ был прерван фильтром безопасности._",
        "en": "\n\n⚠️ _Response was blocked by safety filter._",
    },
    "stream.truncated": {
        "ru": "\n\n⚠️ _Ответ был обрезан из-за ограничения длины._",
        "en": "\n\n⚠️ _Response was truncated due to length limit._",
    },
    "stream.timeout_partial": {
        "ru": "\n\n⏰ _(ответ был прерван по таймауту)_",
        "en": "\n\n⏰ _(response was interrupted by timeout)_",
    },
    "stream.timeout_full": {
        "ru": "⏰ Превышено время ожидания ответа. Попробуйте позже.",
        "en": "⏰ Response timed out. Please try later.",
    },
    "stream.api_error_partial": {
        "ru": "\n\n⚠️ _(ответ был прерван из-за ошибки API)_",
        "en": "\n\n⚠️ _(response was interrupted by API error)_",
    },
    "stream.api_error_full": {
        "ru": "❌ Ошибка API при потоковой генерации. Попробуйте ещё раз.",
        "en": "❌ API error during streaming. Please try again.",
    },
    "stream.generic_error": {
        "ru": "❌ Ошибка при потоковой генерации. Попробуйте ещё раз.",
        "en": "❌ Streaming error. Please try again.",
    },
    # ── Role Management ──────────────────────────────────────────────────
    "role.prompt_updated": {
        "ru": "✅ Промпт роли обновлён!",
        "en": "✅ Role prompt updated!",
    },
    "role.prompt_update_failed": {
        "ru": "❌ Не удалось обновить промпт. Роль не найдена.",
        "en": "❌ Failed to update prompt. Role not found.",
    },
    "role.prompt_update_error": {
        "ru": "❌ Не удалось обновить промпт. Попробуйте позже.",
        "en": "❌ Failed to update prompt. Please try later.",
    },
    "role.ai_enhancing": {
        "ru": "✨ Улучшаю промпт через AI…",
        "en": "✨ Enhancing prompt via AI…",
    },
    "role.ai_no_result": {
        "ru": "❌ AI не вернул результат. Попробуйте ещё раз.",
        "en": "❌ AI returned no result. Please try again.",
    },
    "role.ai_enhanced_preview": {
        "ru": "✨ **Улучшенный промпт** (удерживайте для копирования):\n\n`{prompt}`\n\nСохранить, отредактировать вручную или отменить?",
        "en": "✨ **Enhanced prompt** (hold to copy):\n\n`{prompt}`\n\nSave, edit manually, or cancel?",
    },
    "role.ai_enhance_error": {
        "ru": "❌ Ошибка при улучшении промпта. Попробуйте позже.",
        "en": "❌ Error enhancing prompt. Please try later.",
    },
    "role.btn_save": {
        "ru": "💾 Сохранить",
        "en": "💾 Save",
    },
    "role.btn_edit": {
        "ru": "✏️ Редактировать",
        "en": "✏️ Edit",
    },
    "role.btn_cancel": {
        "ru": "↩️ Отмена",
        "en": "↩️ Cancel",
    },
    "role.renamed": {
        "ru": "✅ Беседа переименована в: {title}",
        "en": "✅ Conversation renamed to: {title}",
    },
    "role.name_length_error": {
        "ru": "❌ Название должно быть от 1 до 100 символов. Попробуйте снова.",
        "en": "❌ Name must be 1-100 characters. Please try again.",
    },
    "role.rename_error": {
        "ru": "❌ Не удалось переименовать беседу. Попробуйте позже.",
        "en": "❌ Failed to rename conversation. Please try later.",
    },
    "role.title_too_long": {
        "ru": "⚠️ Название слишком длинное (макс. 100 символов). Попробуйте короче.",
        "en": "⚠️ Name too long (max 100 chars). Try shorter.",
    },
    "role.title_set": {
        "ru": "✅ Название: **{title}**\n\nТеперь введите **системный промпт** (инструкцию для бота).\nМожно несколько строк — это будет поведение вашей роли:",
        "en": "✅ Name: **{title}**\n\nNow enter the **system prompt** (bot instruction).\nMultiple lines OK — this defines the role behavior:",
    },
    "role.btn_save_apply": {
        "ru": "💾 Сохранить и применить",
        "en": "💾 Save & apply",
    },
    "role.preview_title": {
        "ru": "📋 **Предпросмотр новой роли**\n\n🏷 **Название:** {title}\n📝 **Промпт:**\n`{prompt}`\n\nНажмите кнопку ниже, чтобы сохранить:",
        "en": "📋 **New role preview**\n\n🏷 **Name:** {title}\n📝 **Prompt:**\n`{prompt}`\n\nPress button below to save:",
    },
    "role.no_api_keys": {
        "ru": "❌ Сейчас не удалось создать роль автоматически.\nПопробуйте позже или создайте её вручную.",
        "en": "❌ I couldn’t create the role automatically right now.\nTry again later or create it manually.",
    },
    "role.btn_roles_menu": {
        "ru": "🎭 Меню ролей",
        "en": "🎭 Roles menu",
    },
    "role.generating": {
        "ru": "🛠️ Генерирую роль…",
        "en": "🛠️ Generating role…",
    },
    "role.btn_retry": {
        "ru": "🔄 Попробовать снова",
        "en": "🔄 Try again",
    },
    "role.server_overloaded": {
        "ru": "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд.",
        "en": "🔄 Server overloaded. Please try again in a few seconds.",
    },
    "role.generation_failed": {
        "ru": "❌ Не удалось сгенерировать роль. Попробуйте изменить описание.",
        "en": "❌ Failed to generate role. Try a different description.",
    },
    "role.custom_default_title": {
        "ru": "Кастомная роль",
        "en": "Custom role",
    },
    "role.new_preview": {
        "ru": "🆕 *Новая роль:* {title}\n\n🎯 Цель: {purpose}\n🧭 Стиль: {style}\n\nПрименить сейчас или сохранить?",
        "en": "🆕 *New role:* {title}\n\n🎯 Purpose: {purpose}\n🧭 Style: {style}\n\nApply now or save?",
    },
    "role.btn_apply": {
        "ru": "✅ Применить",
        "en": "✅ Apply",
    },
    "role.btn_retry_custom": {
        "ru": "🔄 Попробовать ещё раз",
        "en": "🔄 Try again",
    },
    "role.generation_error": {
        "ru": "❌ Произошла ошибка при генерации роли.",
        "en": "❌ An error occurred during role generation.",
    },
    "role.role_renamed": {
        "ru": "✅ Роль переименована в: {title}",
        "en": "✅ Role renamed to: {title}",
    },
    "role.role_rename_error": {
        "ru": "❌ Не удалось переименовать роль. Попробуйте позже.",
        "en": "❌ Failed to rename role. Please try later.",
    },
    # ── Messages / Placeholders ──────────────────────────────────────────
    "msg.processing_image": {
        "ru": "🖼️ Обрабатываю изображение...",
        "en": "🖼️ Processing image...",
    },
    "msg.thinking": {
        "ru": "🤔 Думаю...",
        "en": "🤔 Thinking...",
    },
    "msg.rethinking": {
        "ru": "✏️ Обновляю ответ...",
        "en": "✏️ Updating answer...",
    },
    # ── Scheduled Briefs ─────────────────────────────────────────────────
    "brief.morning_title": {
        "ru": "📬 **Утренний бриф**\n\n{summary}",
        "en": "📬 **Morning Brief**\n\n{summary}",
    },
    "brief.subscribed": {
        "ru": "✅ Подписка на <b>{type}</b> активирована!\n📬 Вы будете получать рассылки в {hour}:00 UTC.",
        "en": "✅ Subscribed to <b>{type}</b>!\n📬 You will receive briefs at {hour}:00 UTC.",
    },
    "brief.subscribe_error": {
        "ru": "❌ Ошибка при создании подписки. Попробуйте позже.",
        "en": "❌ Error creating subscription. Please try later.",
    },
    "brief.unsubscribed": {
        "ru": "🔕 Подписка на <b>{type}</b> деактивирована.",
        "en": "🔕 Unsubscribed from <b>{type}</b>.",
    },
    "brief.unsubscribe_error": {
        "ru": "❌ Ошибка при отмене подписки.",
        "en": "❌ Error cancelling subscription.",
    },
    # ── Document Upload ──────────────────────────────────────────────────
    "doc.mode_hint": {
        "ru": "📋 Вы находитесь в режиме работы с документами.\n\n💡 *Доступные действия:*\n• Загрузите новый документ\n• Выберите документ из списка\n• Используйте кнопки под сообщениями\n\n🔄 *Для выхода из режима документов:*\n• Нажмите кнопку '❌ Отменить работу с документами'\n• Или отправьте команду /documents",
        "en": "📋 You are in document mode.\n\n💡 *Available actions:*\n• Upload a new document\n• Select a document from the list\n• Use the buttons below messages\n\n🔄 *To exit document mode:*\n• Press '❌ Cancel document mode'\n• Or send /documents",
    },
    "doc.content_unavailable": {
        "ru": "❌ Не удалось получить содержимое документа.",
        "en": "❌ Could not retrieve document content.",
    },
    "doc.file_too_large": {
        "ru": "❌ Файл слишком большой. Максимальный размер: 50MB.\nПопробуйте файл меньшего размера.",
        "en": "❌ File too large. Max size: 50MB.\nTry a smaller file.",
    },
    "doc.unsupported_format": {
        "ru": "❌ Неподдерживаемый формат файла `.{ext}`.\nОтправьте PDF или DOCX.",
        "en": "❌ Unsupported file format `.{ext}`.\nSend PDF or DOCX.",
    },
    "doc.duplicate_found": {
        "ru": "⚠️ *Файл уже загружен*\n\nФайл `{filename}` уже был загружен ранее как:\n📄 *{dup_name}*\n📅 Загружен: {date}\n\nХотите использовать существующий документ?",
        "en": "⚠️ *File already uploaded*\n\nFile `{filename}` was previously uploaded as:\n📄 *{dup_name}*\n📅 Uploaded: {date}\n\nUse the existing document?",
    },
    "doc.btn_use_existing": {
        "ru": "✅ Использовать существующий",
        "en": "✅ Use existing",
    },
    "doc.btn_upload_new": {
        "ru": "📄 Загрузить как новый",
        "en": "📄 Upload as new",
    },
    "doc.process_failed": {
        "ru": "❌ Не удалось обработать документ. Попробуйте другой файл.",
        "en": "❌ Failed to process document. Try a different file.",
    },
    "doc.success": {
        "ru": "✅ Документ обработан успешно!\n\n📄 *{filename}*\n📊 Страниц: {pages}\n📝 Символов: {chars}\n",
        "en": "✅ Document processed successfully!\n\n📄 *{filename}*\n📊 Pages: {pages}\n📝 Characters: {chars}\n",
    },
    "doc.paragraphs": {
        "ru": "📄 Параграфов: {count}\n",
        "en": "📄 Paragraphs: {count}\n",
    },
    "doc.tables": {
        "ru": "📊 Таблиц: {count}\n",
        "en": "📊 Tables: {count}\n",
    },
    "doc.user_stats": {
        "ru": "\n📋 *Ваши документы:* {count}/5\n",
        "en": "\n📋 *Your documents:* {count}/5\n",
    },
    "doc.limit_reached": {
        "ru": "⚠️ Достигнут лимит документов (5). Старые документы будут автоматически удалены.\n",
        "en": "⚠️ Document limit reached (5). Old documents will be auto-deleted.\n",
    },
    "doc.how_to_ask": {
        "ru": '\n💡 *Как задавать вопросы:*\n• Просто напишите ваш вопрос\n• Например: "Какие основные пункты?", "Что говорится о...?"\n• Система автоматически найдет ответ в документе\n\n📅 *Срок хранения:* 3 дня (автоматическая очистка)',
        "en": '\n💡 *How to ask questions:*\n• Just type your question\n• Example: "What are the key points?", "What does it say about...?"\n• The system will find the answer in the document\n\n📅 *Retention:* 3 days (auto-cleanup)',
    },
    "doc.btn_upload_another": {
        "ru": "📄 Загрузить другой документ",
        "en": "📄 Upload another document",
    },
    "doc.btn_select_document": {
        "ru": "📋 Выбрать документ",
        "en": "📋 Select document",
    },
    "doc.btn_cancel_mode": {
        "ru": "❌ Отменить работу с документами",
        "en": "❌ Cancel document mode",
    },
    # ── Draw / Image Generation UX ───────────────────────────────────────
    "draw.copy_prompt_btn": {
        "ru": "📋 Скопировать промпт",
        "en": "📋 Copy prompt",
    },
    # ── Brief / Digest ───────────────────────────────────────────────────
    "brief.expandable_title": {
        "ru": "<b>📬 Утренний брифинг</b>",
        "en": "<b>📬 Morning Brief</b>",
    },
    # ── Reaction status hints (shown via answer_callback / log) ──────────
    "reaction.thinking": {
        "ru": "🔍 Обрабатываю...",
        "en": "🔍 Processing...",
    },
    "reaction.done": {
        "ru": "⚡ Готово",
        "en": "⚡ Done",
    },
    "reaction.interrupted": {
        "ru": "⚠️ Ответ прерван",
        "en": "⚠️ Response interrupted",
    },
    # ── Expandable blockquote labels ─────────────────────────────────────
    "blockquote.partial_label": {
        "ru": "Частичный ответ / Сбой сети",
        "en": "Partial response / Network failure",
    },
    "miniapp.reconnect_note": {
        "ru": "Изменения применяются через короткое переподключение live-сессии.",
        "en": "Changes apply after a short reconnect of the live session.",
    },
}


def t(key: str, lang: str = DEFAULT_LANG, **kwargs: str) -> str:
    """Translate a key to the given language.

    Args:
        key: Dot-separated string key (e.g. "voice.processing").
        lang: Language code ("ru" or "en").
        **kwargs: Optional format variables (e.g. count=3).

    Returns:
        Translated string, or the key itself if not found.
    """
    entry = _STRINGS.get(key)
    if entry is None:
        return key

    text = entry.get(lang, entry.get(DEFAULT_LANG, key))

    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass  # Return unformatted if vars mismatch

    return text
