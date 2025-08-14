# /app/state.py

import asyncio
import weakref
from collections import defaultdict
from threading import Lock
from typing import Dict, Set, Optional

class UserLockManager:
    """Безопасный менеджер блокировок для пользователей"""
    def __init__(self):
        self._locks = weakref.WeakValueDictionary()
        self._global_lock = Lock()
    
    def get_lock(self, user_id: int) -> asyncio.Lock:
        """Получает блокировку для пользователя, создавая новую при необходимости"""
        with self._global_lock:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]
    
    def cleanup(self):
        """Очищает неиспользуемые блокировки"""
        with self._global_lock:
            # WeakValueDictionary автоматически очищает неиспользуемые объекты
            pass

# Глобальный экземпляр менеджера блокировок
lock_manager = UserLockManager()

# Функция для получения блокировки пользователя
def get_user_lock(user_id: int) -> asyncio.Lock:
    """Получает блокировку для пользователя"""
    return lock_manager.get_lock(user_id)

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
