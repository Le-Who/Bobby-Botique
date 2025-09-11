# /app/state.py

import asyncio
from collections import defaultdict
from typing import Dict, Set, Optional

# Комплексное состояние пользователя
class UserState:
    """Содержит состояние и блокировки для одного пользователя"""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.document_mode = False
        self.selected_document_id: Optional[int] = None
        self.last_document_message_id: Optional[int] = None
        # Кастомные роли
        self.awaiting_custom_role_input: bool = False
        self.generated_role: Optional[dict] = None
        self.last_custom_role_prompt: Optional[str] = None
        self.generating_custom_role: bool = False

# Хранилище состояний пользователей
USER_STATES: Dict[int, UserState] = defaultdict(UserState)

# Хранилище блокировок пользователей (для обратной совместимости)
USER_LOCKS: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def get_user_state(user_id: int) -> UserState:
    """Получает комплексное состояние пользователя"""
    return USER_STATES[user_id]

def get_user_lock(user_id: int) -> asyncio.Lock:
    """Получает блокировку для пользователя"""
    return get_user_state(user_id).lock

def get_user_locks(user_id: int) -> asyncio.Lock:
    """Получает блокировку для пользователя (для обратной совместимости)"""
    return USER_LOCKS[user_id]

def set_document_mode(user_id: int, enabled: bool, document_id: Optional[int] = None):
    """Устанавливает режим работы с документами для пользователя"""
    state = get_user_state(user_id)
    state.document_mode = enabled
    state.selected_document_id = document_id if enabled else None

def clear_document_state(user_id: int):
    """Очищает состояние работы с документами для пользователя"""
    state = get_user_state(user_id)
    state.document_mode = False
    state.selected_document_id = None
    state.last_document_message_id = None

def begin_custom_role_creation(user_id: int):
    state = get_user_state(user_id)
    state.awaiting_custom_role_input = True
    state.generated_role = None
    state.last_custom_role_prompt = None
    state.generating_custom_role = False

def set_generated_role(user_id: int, role: dict):
    state = get_user_state(user_id)
    state.generated_role = role
    state.awaiting_custom_role_input = False

def clear_custom_role_state(user_id: int):
    state = get_user_state(user_id)
    state.awaiting_custom_role_input = False
    state.generated_role = None
    state.last_custom_role_prompt = None
    state.generating_custom_role = False

def set_last_custom_role_prompt(user_id: int, prompt: str):
    state = get_user_state(user_id)
    state.last_custom_role_prompt = prompt

def get_last_custom_role_prompt(user_id: int) -> Optional[str]:
    return get_user_state(user_id).last_custom_role_prompt

def set_generating_custom_role(user_id: int, value: bool):
    state = get_user_state(user_id)
    state.generating_custom_role = value

def is_awaiting_custom_role_input(user_id: int) -> bool:
    return get_user_state(user_id).awaiting_custom_role_input

def get_generated_role(user_id: int) -> Optional[dict]:
    return get_user_state(user_id).generated_role

def is_in_document_mode(user_id: int) -> bool:
    """Проверяет, находится ли пользователь в режиме работы с документами"""
    return get_user_state(user_id).document_mode

def get_selected_document_id(user_id: int) -> Optional[int]:
    """Получает ID выбранного документа пользователя"""
    return get_user_state(user_id).selected_document_id
