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
    """
    if not text or len(text.strip()) < 3:
        return DEFAULT_LANG

    cyrillic_count = len(_CYRILLIC_RE.findall(text))
    alpha_count = sum(1 for ch in text if ch.isalpha())

    if alpha_count == 0:
        return DEFAULT_LANG

    return "ru" if (cyrillic_count / alpha_count) >= 0.2 else "en"


# ── String Registry ──────────────────────────────────────────────────────────

_STRINGS: dict[str, dict[str, str]] = {
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
        "ru": "❌ Нет доступных ключей API. Пожалуйста, попробуйте позже.",
        "en": "❌ No API keys available. Please try later.",
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

    # ── Help ─────────────────────────────────────────────────────────────
    "help.title": {
        "ru": (
            "📚 **Справка**\n\n"
            "💬 **Чат** — просто напишите сообщение\n"
            "🌐 **Поиск** — `?` или `??` перед вопросом\n"
            "📄 **Документы** — отправьте PDF/DOCX\n"
            "🎭 **Роли** — специализация бота\n\n"
            "Нажмите кнопку для подробностей:"
        ),
        "en": (
            "📚 **Help**\n\n"
            "💬 **Chat** — just type a message\n"
            "🌐 **Search** — `?` or `??` before your question\n"
            "📄 **Documents** — send a PDF/DOCX\n"
            "🎭 **Roles** — bot specialization\n\n"
            "Click a button for details:"
        ),
    },
    "help.btn_chat": {
        "ru": "💬 Чат",
        "en": "💬 Chat",
    },
    "help.btn_search": {
        "ru": "🌐 Поиск",
        "en": "🌐 Search",
    },
    "help.btn_docs": {
        "ru": "📄 Документы",
        "en": "📄 Documents",
    },
    "help.btn_roles": {
        "ru": "🎭 Роли",
        "en": "🎭 Roles",
    },
    "help.back_to_help": {
        "ru": "⬅️ К справке",
        "en": "⬅️ Back to help",
    },
    "help.topic_not_found": {
        "ru": "❓ Тема не найдена.",
        "en": "❓ Topic not found.",
    },
    "help.topic.chat": {
        "ru": (
            "💬 **Как общаться**\n\n"
            "Просто напишите сообщение в чат — бот ответит "
            "с помощью AI.\n\n"
            "• Отправьте 🖼️ фото — бот проанализирует изображение\n"
            "• `/newchat` — начать новый диалог\n"
            "• `/setprompt` — задать системную инструкцию\n"
            "• `/save` — сохранить текущую беседу"
        ),
        "en": (
            "💬 **How to chat**\n\n"
            "Just type a message — the bot will respond "
            "using AI.\n\n"
            "• Send a 🖼️ photo — the bot will analyze the image\n"
            "• `/newchat` — start a new dialog\n"
            "• `/setprompt` — set a system instruction\n"
            "• `/save` — save the current conversation"
        ),
    },
    "help.topic.search": {
        "ru": (
            "🌐 **Поиск в интернете**\n\n"
            "• `? вопрос` — быстрый фактический ответ\n"
            "• `?? вопрос` — глубокое исследование с источниками\n"
            "• `??` + фото — поиск по изображению\n\n"
            "💡 `/res` — включить/выключить поиск для всех сообщений"
        ),
        "en": (
            "🌐 **Web search**\n\n"
            "• `? question` — quick factual answer\n"
            "• `?? question` — deep research with sources\n"
            "• `??` + photo — image search\n\n"
            "💡 `/res` — toggle search for all messages"
        ),
    },
    "help.topic.docs": {
        "ru": (
            "📄 **Работа с документами**\n\n"
            "Отправьте PDF или DOCX файл в чат — "
            "бот извлечёт текст и будет отвечать "
            "на основе содержимого.\n\n"
            "• Максимум: 5 документов\n"
            "• Хранение: 3 дня\n"
            "• `/documents` — управление документами"
        ),
        "en": (
            "📄 **Working with documents**\n\n"
            "Send a PDF or DOCX file — "
            "the bot will extract text and answer "
            "based on the content.\n\n"
            "• Maximum: 5 documents\n"
            "• Storage: 3 days\n"
            "• `/documents` — manage documents"
        ),
    },
    "help.topic.roles": {
        "ru": (
            "🎭 **Роли**\n\n"
            "Роль — это специализация бота: он будет "
            "отвечать как эксперт в выбранной области.\n\n"
            "• 6 готовых ролей: преподаватель, IT-инженер, доктор…\n"
            "• ✨ Сгенерировать роль по описанию\n"
            "• 📝 Написать свою вручную\n"
            "• `/roles` — открыть меню ролей"
        ),
        "en": (
            "🎭 **Roles**\n\n"
            "A role specializes the bot as "
            "an expert in a chosen field.\n\n"
            "• 6 preset roles: teacher, IT engineer, doctor…\n"
            "• ✨ Generate a role from a description\n"
            "• 📝 Write your own manually\n"
            "• `/roles` — open roles menu"
        ),
    },

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
