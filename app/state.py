# /app/state.py

import asyncio
from collections import defaultdict
from typing import Dict, Set, Optional

# Блокировки для пользователей
USER_LOCKS: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Состояния пользователей для работы с документами
class UserDocumentState:
    """Состояние пользователя для работы с документами"""
    def __init__(self):
        self.document_mode = False  # Режим работы с документами
        self.selected_document_id: Optional[int] = None  # ID выбранного документа
        self.last_document_message_id: Optional[int] = None  # ID последнего сообщения с кнопками документов

# Хранилище состояний пользователей
USER_DOCUMENT_STATES: Dict[int, UserDocumentState] = defaultdict(UserDocumentState)

def get_user_document_state(user_id: int) -> UserDocumentState:
    """Получает состояние пользователя для работы с документами"""
    return USER_DOCUMENT_STATES[user_id]

def set_document_mode(user_id: int, enabled: bool, document_id: Optional[int] = None):
    """Устанавливает режим работы с документами для пользователя"""
    state = get_user_document_state(user_id)
    state.document_mode = enabled
    state.selected_document_id = document_id if enabled else None

def clear_document_state(user_id: int):
    """Очищает состояние работы с документами для пользователя"""
    state = get_user_document_state(user_id)
    state.document_mode = False
    state.selected_document_id = None
    state.last_document_message_id = None

def is_in_document_mode(user_id: int) -> bool:
    """Проверяет, находится ли пользователь в режиме работы с документами"""
    return get_user_document_state(user_id).document_mode

def get_selected_document_id(user_id: int) -> Optional[int]:
    """Получает ID выбранного документа пользователя"""
    return get_user_document_state(user_id).selected_document_id
