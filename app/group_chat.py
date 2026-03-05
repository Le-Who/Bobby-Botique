import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app import database as db
from app.repos.users import is_authorized as _is_authorized


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
    settings: dict[str, Any]


class GroupChatManager:
    """Менеджер групповых чатов"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized"):
            self.active_groups: dict[int, GroupChat] = {}
            self.group_settings: dict[int, dict[str, Any]] = {}
            self.user_groups: dict[int, set[int]] = defaultdict(set)  # user_id -> set of chat_ids
            self._lock = asyncio.Lock()
            self.initialized = True

    async def initialize(self):
        """Инициализирует менеджер групповых чатов"""
        try:
            # Create таблицы for групповых chatов
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

            # Load active groups
            await self._load_active_groups()

            logging.info("Group chat manager initialized")

        except Exception as e:
            logging.error("Error initializing group chat manager: %s", e, exc_info=True)

    async def _load_active_groups(self):
        """Загружает активные группы из базы данных"""
        try:
            result = await db.db_query(
                "SELECT chat_id, title, is_active, created_at, last_activity,"
                " member_count, admin_user_id, settings"
                " FROM group_chats WHERE is_active = TRUE"
            )

            chat_ids = []
            for row in result:
                group = GroupChat(
                    chat_id=row["chat_id"],
                    title=row["title"],
                    is_active=row["is_active"],
                    created_at=row["created_at"],
                    last_activity=row["last_activity"],
                    member_count=row["member_count"],
                    admin_user_id=row["admin_user_id"],
                    settings=row.get("settings", {}),
                )

                self.active_groups[group.chat_id] = group
                self.group_settings[group.chat_id] = group.settings
                chat_ids.append(group.chat_id)

            if chat_ids:
                # Load участников всех активных групп одним requestом
                members = await db.db_query(
                    "SELECT chat_id, user_id FROM group_members WHERE chat_id = ANY($1::bigint[])",
                    (chat_ids,),
                )

                for member in members:
                    self.user_groups[member["user_id"]].add(member["chat_id"])

            logging.info("Loaded %s active groups", len(self.active_groups))

        except Exception as e:
            logging.error("Error loading active groups: %s", e, exc_info=True)

    async def register_group(self, chat_id: int, title: str, admin_user_id: int) -> bool:
        """Регистрирует новую группу"""
        try:
            # Check, что user авторfromован
            if not await _is_authorized(admin_user_id):
                return False

            async with self._lock:
                # Check, не зарегистрирована ли уже group
                if chat_id in self.active_groups:
                    return False

                # Create группу в базе данных
                await db.db_query(
                    "INSERT INTO group_chats (chat_id, title, admin_user_id) VALUES ($1, $2, $3)",
                    (chat_id, title, admin_user_id),
                )

                # Add администратора как участника
                await db.db_query(
                    "INSERT INTO group_members (chat_id, user_id, is_admin) VALUES ($1, $2, TRUE)",
                    (chat_id, admin_user_id),
                )

                # Create объект groups
                group = GroupChat(
                    chat_id=chat_id,
                    title=title,
                    is_active=True,
                    created_at=datetime.now(),
                    last_activity=datetime.now(),
                    member_count=1,
                    admin_user_id=admin_user_id,
                    settings={},
                )

                self.active_groups[chat_id] = group
                self.group_settings[chat_id] = {}
                self.user_groups[admin_user_id].add(chat_id)

                logging.info("Registered new group: %s (ID: %s)", title, chat_id)
                return True

        except Exception as e:
            logging.error("Error registering group: %s", e, exc_info=True)
            return False

    async def add_member_to_group(self, chat_id: int, user_id: int) -> bool:
        """Добавляет участника в группу (атомарно)"""
        try:
            if chat_id not in self.active_groups:
                return False

            # Check, что user авторfromован
            if not await _is_authorized(user_id):
                return False

            async with self._lock:
                # Add в базу данных — RETURNING confirms actual insert
                result = await db.db_query(
                    "INSERT INTO group_members (chat_id, user_id) VALUES ($1, $2) ON CONFLICT (chat_id, user_id) DO NOTHING RETURNING user_id",
                    (chat_id, user_id),
                )

                if result:
                    # Атомарно обновляем количество участников
                    await db.db_query(
                        "UPDATE group_chats SET member_count = member_count + 1 WHERE chat_id = $1",
                        (chat_id,),
                    )

                # Update в памяти
                self.user_groups[user_id].add(chat_id)
                if result and chat_id in self.active_groups:
                    self.active_groups[chat_id].member_count += 1

                return True

        except Exception as e:
            logging.error("Error adding member to group: %s", e, exc_info=True)
            return False

    async def remove_member_from_group(self, chat_id: int, user_id: int) -> bool:
        """Удаляет участника из группы (атомарно)"""
        try:
            if chat_id not in self.active_groups:
                return False

            async with self._lock:
                # Delete from базы данных
                await db.db_query(
                    "DELETE FROM group_members WHERE chat_id = $1 AND user_id = $2",
                    (chat_id, user_id),
                )

                # Атомарно обновляем количество участников
                await db.db_query(
                    "UPDATE group_chats SET member_count = member_count - 1 WHERE chat_id = $1 AND member_count > 0",
                    (chat_id,),
                )

                # Update в памяти
                self.user_groups[user_id].discard(chat_id)
                if chat_id in self.active_groups:
                    self.active_groups[chat_id].member_count = max(0, self.active_groups[chat_id].member_count - 1)

                return True

        except Exception as e:
            logging.error("Error removing member from group: %s", e, exc_info=True)
            return False

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        """Проверяет, является ли пользователь участником группы"""
        return chat_id in self.user_groups.get(user_id, set())

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором группы"""
        try:
            result = await db.db_query(
                "SELECT is_admin FROM group_members WHERE chat_id = $1 AND user_id = $2",
                (chat_id, user_id),
            )

            return result and result[0]["is_admin"]

        except Exception as e:
            logging.error("Error checking admin status: %s", e, exc_info=True)
            return False

    async def get_group_info(self, chat_id: int) -> GroupChat | None:
        """Получает информацию о группе"""
        return self.active_groups.get(chat_id)

    async def get_user_groups(self, user_id: int) -> list[GroupChat]:
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
                    (chat_id,),
                )

        except Exception as e:
            logging.error("Error updating group activity: %s", e, exc_info=True)

    async def log_group_message(
        self,
        chat_id: int,
        user_id: int,
        message_text: str,
        message_type: str = "text",
        is_bot_response: bool = False,
    ):
        """Логирует сообщение в группе"""
        try:
            await db.db_query(
                "INSERT INTO group_messages (chat_id, user_id, message_text, message_type, is_bot_response) VALUES ($1, $2, $3, $4, $5)",
                (chat_id, user_id, message_text, message_type, is_bot_response),
            )

            await self.update_group_activity(chat_id)

        except Exception as e:
            logging.error("Error logging group message: %s", e, exc_info=True)

    async def get_group_stats(self, chat_id: int) -> dict[str, Any]:
        """Получает статистику группы"""
        try:
            # Общее количество сообщений
            total_messages = await db.db_query(
                "SELECT COUNT(*) as count FROM group_messages WHERE chat_id = $1",
                (chat_id,),
            )

            # Сообщения за afterдние 24 часа
            recent_messages = await db.db_query(
                "SELECT COUNT(*) as count FROM group_messages WHERE chat_id = $1 AND created_at > NOW() - INTERVAL '24 hours'",
                (chat_id,),
            )

            # Активные пользователи за afterдние 24 часа
            active_users = await db.db_query(
                "SELECT COUNT(DISTINCT user_id) as count FROM group_messages WHERE chat_id = $1 AND created_at > NOW() - INTERVAL '24 hours'",
                (chat_id,),
            )

            return {
                "total_messages": total_messages[0]["count"] if total_messages else 0,
                "recent_messages": recent_messages[0]["count"] if recent_messages else 0,
                "active_users_24h": active_users[0]["count"] if active_users else 0,
                "member_count": self.active_groups[chat_id].member_count if chat_id in self.active_groups else 0,
            }

        except Exception as e:
            logging.error("Error getting group stats: %s", e, exc_info=True)
            return {}


# Глобальный экземпляр менеджера групповых chatов
group_chat_manager = GroupChatManager()


async def initialize_group_chats():
    """Инициализирует систему групповых чатов"""
    await group_chat_manager.initialize()


async def register_new_group(chat_id: int, title: str, admin_user_id: int) -> bool:
    """Регистрирует новую группу"""
    return await group_chat_manager.register_group(chat_id, title, admin_user_id)


async def add_group_member(chat_id: int, user_id: int) -> bool:
    """Добавляет участника в группу"""
    return await group_chat_manager.add_member_to_group(chat_id, user_id)


async def is_group_member(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь участником группы"""
    return await group_chat_manager.is_member(chat_id, user_id)


async def is_group_admin(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором группы"""
    return await group_chat_manager.is_admin(chat_id, user_id)


async def get_group_info(chat_id: int) -> GroupChat | None:
    """Получает информацию о группе"""
    return await group_chat_manager.get_group_info(chat_id)


async def log_group_message(
    chat_id: int,
    user_id: int,
    message_text: str,
    message_type: str = "text",
    is_bot_response: bool = False,
):
    """Логирует сообщение в группе"""
    await group_chat_manager.log_group_message(chat_id, user_id, message_text, message_type, is_bot_response)
