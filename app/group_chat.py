import logging
import asyncio
from typing import Dict, Any, List, Optional, Set
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import defaultdict

from .config import settings
from . import database as db
from .metrics import metrics_collector

@dataclass
class GroupChat:
    """Информация о групповом чате"""
    chat_id: int
    title: str
    is_active: bool
    created_at: datetime
    last_activity: datetime
    member_count: int
    admin_user_id: int
    settings: Dict[str, Any]

class GroupChatManager:
    """Менеджер групповых чатов"""
    
    def __init__(self):
        self.active_groups: Dict[int, GroupChat] = {}
        self.group_settings: Dict[int, Dict[str, Any]] = {}
        self.user_groups: Dict[int, Set[int]] = defaultdict(set)  # user_id -> set of chat_ids
        self._lock = asyncio.Lock()
    
    async def initialize(self):
        """Инициализирует менеджер групповых чатов"""
        try:
            # Создаем таблицы для групповых чатов
            await db.db_query("""
                CREATE TABLE IF NOT EXISTS group_chats (
                    chat_id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    member_count INTEGER DEFAULT 0,
                    admin_user_id BIGINT NOT NULL,
                    settings JSONB DEFAULT '{}'
                )
            """)
            
            await db.db_query("""
                CREATE TABLE IF NOT EXISTS group_members (
                    chat_id BIGINT,
                    user_id BIGINT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_admin BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            
            await db.db_query("""
                CREATE TABLE IF NOT EXISTS group_messages (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    message_text TEXT,
                    message_type TEXT DEFAULT 'text',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_bot_response BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Загружаем активные группы
            await self._load_active_groups()
            
            logging.info("Group chat manager initialized")
            
        except Exception as e:
            logging.error(f"Error initializing group chat manager: {e}")
    
    async def _load_active_groups(self):
        """Загружает активные группы из базы данных"""
        try:
            result = await db.db_query(
                "SELECT * FROM group_chats WHERE is_active = TRUE"
            )
            
            for row in result:
                group = GroupChat(
                    chat_id=row['chat_id'],
                    title=row['title'],
                    is_active=row['is_active'],
                    created_at=row['created_at'],
                    last_activity=row['last_activity'],
                    member_count=row['member_count'],
                    admin_user_id=row['admin_user_id'],
                    settings=row.get('settings', {})
                )
                
                self.active_groups[group.chat_id] = group
                self.group_settings[group.chat_id] = group.settings
                
                # Загружаем участников группы
                members = await db.db_query(
                    "SELECT user_id FROM group_members WHERE chat_id = $1",
                    (group.chat_id,)
                )
                
                for member in members:
                    self.user_groups[member['user_id']].add(group.chat_id)
            
            logging.info(f"Loaded {len(self.active_groups)} active groups")
            
        except Exception as e:
            logging.error(f"Error loading active groups: {e}")
    
    async def register_group(self, chat_id: int, title: str, admin_user_id: int) -> bool:
        """Регистрирует новую группу"""
        try:
            # Проверяем, что пользователь авторизован
            if not await db.is_authorized(admin_user_id):
                return False
            
            async with self._lock:
                # Проверяем, не зарегистрирована ли уже группа
                if chat_id in self.active_groups:
                    return False
                
                # Создаем группу в базе данных
                await db.db_query(
                    "INSERT INTO group_chats (chat_id, title, admin_user_id) VALUES ($1, $2, $3)",
                    (chat_id, title, admin_user_id)
                )
                
                # Добавляем администратора как участника
                await db.db_query(
                    "INSERT INTO group_members (chat_id, user_id, is_admin) VALUES ($1, $2, TRUE)",
                    (chat_id, admin_user_id)
                )
                
                # Создаем объект группы
                group = GroupChat(
                    chat_id=chat_id,
                    title=title,
                    is_active=True,
                    created_at=datetime.now(),
                    last_activity=datetime.now(),
                    member_count=1,
                    admin_user_id=admin_user_id,
                    settings={}
                )
                
                self.active_groups[chat_id] = group
                self.group_settings[chat_id] = {}
                self.user_groups[admin_user_id].add(chat_id)
                
                logging.info(f"Registered new group: {title} (ID: {chat_id})")
                return True
                
        except Exception as e:
            logging.error(f"Error registering group: {e}")
            return False
    
    async def add_member(self, chat_id: int, user_id: int) -> bool:
        """Добавляет участника в группу"""
        try:
            if chat_id not in self.active_groups:
                return False
            
            # Проверяем, что пользователь авторизован
            if not await db.is_authorized(user_id):
                return False
            
            async with self._lock:
                # Добавляем в базу данных
                await db.db_query(
                    "INSERT INTO group_members (chat_id, user_id) VALUES ($1, $2) ON CONFLICT (chat_id, user_id) DO NOTHING",
                    (chat_id, user_id)
                )
                
                # Обновляем в памяти
                self.user_groups[user_id].add(chat_id)
                self.active_groups[chat_id].member_count += 1
                
                # Обновляем количество участников в базе
                await db.db_query(
                    "UPDATE group_chats SET member_count = member_count + 1 WHERE chat_id = $1",
                    (chat_id,)
                )
                
                return True
                
        except Exception as e:
            logging.error(f"Error adding member to group: {e}")
            return False
    
    async def remove_member(self, chat_id: int, user_id: int) -> bool:
        """Удаляет участника из группы"""
        try:
            if chat_id not in self.active_groups:
                return False
            
            async with self._lock:
                # Удаляем из базы данных
                await db.db_query(
                    "DELETE FROM group_members WHERE chat_id = $1 AND user_id = $2",
                    (chat_id, user_id)
                )
                
                # Обновляем в памяти
                self.user_groups[user_id].discard(chat_id)
                self.active_groups[chat_id].member_count = max(0, self.active_groups[chat_id].member_count - 1)
                
                # Обновляем количество участников в базе
                await db.db_query(
                    "UPDATE group_chats SET member_count = member_count - 1 WHERE chat_id = $1",
                    (chat_id,)
                )
                
                return True
                
        except Exception as e:
            logging.error(f"Error removing member from group: {e}")
            return False
    
    async def is_member(self, chat_id: int, user_id: int) -> bool:
        """Проверяет, является ли пользователь участником группы"""
        return chat_id in self.user_groups.get(user_id, set())
    
    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором группы"""
        try:
            result = await db.db_query(
                "SELECT is_admin FROM group_members WHERE chat_id = $1 AND user_id = $2",
                (chat_id, user_id)
            )
            
            return result and result[0]['is_admin']
            
        except Exception as e:
            logging.error(f"Error checking admin status: {e}")
            return False
    
    async def get_group_info(self, chat_id: int) -> Optional[GroupChat]:
        """Получает информацию о группе"""
        return self.active_groups.get(chat_id)
    
    async def get_user_groups(self, user_id: int) -> List[GroupChat]:
        """Получает группы пользователя"""
        group_ids = self.user_groups.get(user_id, set())
        return [self.active_groups[gid] for gid in group_ids if gid in self.active_groups]
    
    async def update_group_activity(self, chat_id: int):
        """Обновляет время последней активности группы"""
        try:
            if chat_id in self.active_groups:
                self.active_groups[chat_id].last_activity = datetime.now()
                
                await db.db_query(
                    "UPDATE group_chats SET last_activity = CURRENT_TIMESTAMP WHERE chat_id = $1",
                    (chat_id,)
                )
                
        except Exception as e:
            logging.error(f"Error updating group activity: {e}")
    
    async def log_group_message(self, chat_id: int, user_id: int, message_text: str, 
                               message_type: str = 'text', is_bot_response: bool = False):
        """Логирует сообщение в группе"""
        try:
            await db.db_query(
                "INSERT INTO group_messages (chat_id, user_id, message_text, message_type, is_bot_response) VALUES ($1, $2, $3, $4, $5)",
                (chat_id, user_id, message_text, message_type, is_bot_response)
            )
            
            await self.update_group_activity(chat_id)
            
        except Exception as e:
            logging.error(f"Error logging group message: {e}")
    
    async def get_group_stats(self, chat_id: int) -> Dict[str, Any]:
        """Получает статистику группы"""
        try:
            # Общее количество сообщений
            total_messages = await db.db_query(
                "SELECT COUNT(*) as count FROM group_messages WHERE chat_id = $1",
                (chat_id,)
            )
            
            # Сообщения за последние 24 часа
            recent_messages = await db.db_query(
                "SELECT COUNT(*) as count FROM group_messages WHERE chat_id = $1 AND created_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'",
                (chat_id,)
            )
            
            # Активные пользователи за последние 24 часа
            active_users = await db.db_query(
                "SELECT COUNT(DISTINCT user_id) as count FROM group_messages WHERE chat_id = $1 AND created_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'",
                (chat_id,)
            )
            
            return {
                'total_messages': total_messages[0]['count'] if total_messages else 0,
                'recent_messages': recent_messages[0]['count'] if recent_messages else 0,
                'active_users_24h': active_users[0]['count'] if active_users else 0,
                'member_count': self.active_groups[chat_id].member_count if chat_id in self.active_groups else 0
            }
            
        except Exception as e:
            logging.error(f"Error getting group stats: {e}")
            return {}

# Глобальный экземпляр менеджера групповых чатов
group_chat_manager = GroupChatManager()

async def initialize_group_chats():
    """Инициализирует систему групповых чатов"""
    await group_chat_manager.initialize()

async def register_new_group(chat_id: int, title: str, admin_user_id: int) -> bool:
    """Регистрирует новую группу"""
    return await group_chat_manager.register_group(chat_id, title, admin_user_id)

async def add_group_member(chat_id: int, user_id: int) -> bool:
    """Добавляет участника в группу"""
    return await group_chat_manager.add_member(chat_id, user_id)

async def is_group_member(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь участником группы"""
    return await group_chat_manager.is_member(chat_id, user_id)

async def is_group_admin(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором группы"""
    return await group_chat_manager.is_admin(chat_id, user_id)

async def get_group_info(chat_id: int) -> Optional[GroupChat]:
    """Получает информацию о группе"""
    return await group_chat_manager.get_group_info(chat_id)

async def log_group_message(chat_id: int, user_id: int, message_text: str, 
                           message_type: str = 'text', is_bot_response: bool = False):
    """Логирует сообщение в группе"""
    await group_chat_manager.log_group_message(chat_id, user_id, message_text, message_type, is_bot_response) 