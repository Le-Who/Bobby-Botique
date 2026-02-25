"""
Processing stage indicators for user-facing status updates.

Provides animated, contextual status messages during multi-step AI operations.
Each stage replaces the placeholder message text as the bot progresses through
search → analysis → response generation.
"""

import logging
from typing import Optional


# ── Stage definitions ──────────────────────────────────────────────────────────

STAGES_CHAT = [
    ("🤔", "Думаю..."),
    ("💭", "Формирую ответ..."),
]

STAGES_SEARCH_QUICK = [
    ("🔎", "Ищу быстрый ответ..."),
    ("📊", "Анализирую результаты..."),
    ("💡", "Формулирую ответ..."),
]

STAGES_SEARCH_DEEP = [
    ("🔎", "Ищу источники..."),
    ("📚", "Изучаю найденное..."),
    ("🧠", "Синтезирую информацию..."),
    ("📝", "Пишу развёрнутый ответ..."),
]

STAGES_PHOTO = [
    ("🖼️", "Обрабатываю изображение..."),
    ("👁️", "Анализирую содержимое..."),
    ("💬", "Формирую описание..."),
]

STAGES_DOCUMENT = [
    ("📄", "Читаю документ..."),
    ("🔍", "Анализирую содержимое..."),
    ("💬", "Формирую ответ..."),
]


async def update_stage(
    message,
    stage_list: list,
    stage_index: int,
    extra_text: Optional[str] = None,
) -> int:
    """
    Update the placeholder message to show the current processing stage.

    Args:
        message: Telegram Message object to edit
        stage_list: List of (emoji, text) stage tuples
        stage_index: Current stage index (0-based)
        extra_text: Optional additional context line

    Returns:
        Next stage index (capped at len(stage_list) - 1)
    """
    if stage_index >= len(stage_list):
        stage_index = len(stage_list) - 1

    emoji, text = stage_list[stage_index]
    full_text = f"{emoji} {text}"
    if extra_text:
        full_text += f"\n{extra_text}"

    try:
        await message.edit_text(full_text)
    except Exception as e:
        # Silently handle "message not modified" and similar
        logging.debug(f"Stage indicator update skipped: {e}")

    return min(stage_index + 1, len(stage_list) - 1)
