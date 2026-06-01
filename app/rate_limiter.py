"""
Модуль ограничения частоты запросов (Rate Limiting)
"""
import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
import weakref

from .config import settings


class RateLimiter:
    """Ограничитель частоты запросов для пользователей"""

    def __init__(self, requests_per_minute: int = 20, cleanup_interval: int = 300):
        self.requests_per_minute = requests_per_minute
        self.cleanup_interval = cleanup_interval  # 5 минут

        # Используем deque для эффективного удаления старых записей
        self.user_requests: Dict[int, deque] = defaultdict(deque)
        self.blocked_users: Dict[int, float] = {}  # user_id -> unblock_timestamp

        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._initialized = False

        # НЕ запускаем cleanup task здесь - event loop еще не готов
        # Задача будет запущена позже через start_cleanup()

    def start_cleanup(self):
        """Запускает задачу очистки (вызывается когда event loop готов)"""
        if self._initialized:
            return

        try:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._initialized = True
            logging.info("Rate limiter cleanup task started")
        except Exception as e:
            logging.error(f"Failed to start rate limiter cleanup task: {e}")

    def _start_cleanup_task(self):
        """Устаревший метод - оставлен для совместимости"""
        pass

    async def _cleanup_loop(self):
        """Периодически очищает старые записи"""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_old_records()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in rate limiter cleanup: {e}")

    async def _cleanup_old_records(self):
        """Удаляет старые записи запросов"""
        current_time = time.time()
        cutoff_time = current_time - 60  # Удаляем записи старше 1 минуты

        async with self._lock:
            # Очищаем старые запросы
            users_to_remove = []
            for user_id, requests in self.user_requests.items():
                # Удаляем запросы старше минуты
                while requests and requests[0] < cutoff_time:
                    requests.popleft()

                # Если у пользователя нет запросов, удаляем его из словаря
                if not requests:
                    users_to_remove.append(user_id)

            for user_id in users_to_remove:
                del self.user_requests[user_id]

            # Очищаем разблокированных пользователей
            unblocked_users = [
                user_id for user_id, unblock_time in self.blocked_users.items()
                if current_time >= unblock_time
            ]

            for user_id in unblocked_users:
                del self.blocked_users[user_id]
                logging.info(f"User {user_id} unblocked after rate limit timeout")

            if users_to_remove or unblocked_users:
                logging.debug(f"Rate limiter cleanup: removed {len(users_to_remove)} inactive users, unblocked {len(unblocked_users)} users")

    async def is_allowed(self, user_id: int) -> Tuple[bool, Optional[str]]:
        """
        Проверяет, разрешен ли запрос для пользователя

        Returns:
            Tuple[bool, Optional[str]]: (разрешен_ли_запрос, сообщение_об_ошибке)
        """
        current_time = time.time()

        async with self._lock:
            # Проверяем, заблокирован ли пользователь
            if user_id in self.blocked_users:
                unblock_time = self.blocked_users[user_id]
                if current_time < unblock_time:
                    remaining_time = int(unblock_time - current_time)
                    return False, f"🚫 Превышен лимит запросов. Попробуйте через {remaining_time} секунд."
                else:
                    # Время блокировки истекло
                    del self.blocked_users[user_id]

            # Получаем запросы пользователя за последнюю минуту
            user_requests = self.user_requests[user_id]
            minute_ago = current_time - 60

            # Удаляем запросы старше минуты
            while user_requests and user_requests[0] < minute_ago:
                user_requests.popleft()

            # Проверяем лимит
            if len(user_requests) >= self.requests_per_minute:
                # Блокируем пользователя на 1 минуту
                self.blocked_users[user_id] = current_time + 60
                logging.warning(f"User {user_id} rate limited: {len(user_requests)} requests in last minute")
                return False, f"🚫 Превышен лимит {self.requests_per_minute} запросов в минуту. Попробуйте через минуту."

            # Добавляем текущий запрос
            user_requests.append(current_time)
            return True, None

    async def get_user_stats(self, user_id: int) -> Dict[str, any]:
        """Получает статистику запросов пользователя"""
        current_time = time.time()
        minute_ago = current_time - 60

        async with self._lock:
            user_requests = self.user_requests.get(user_id, deque())

            # Подсчитываем запросы за последнюю минуту
            recent_requests = sum(1 for req_time in user_requests if req_time > minute_ago)

            is_blocked = user_id in self.blocked_users
            unblock_time = self.blocked_users.get(user_id, 0)
            remaining_block_time = max(0, int(unblock_time - current_time)) if is_blocked else 0

            return {
                'user_id': user_id,
                'requests_last_minute': recent_requests,
                'requests_limit': self.requests_per_minute,
                'is_blocked': is_blocked,
                'remaining_block_time': remaining_block_time,
                'utilization_percent': (recent_requests / self.requests_per_minute) * 100
            }

    async def get_global_stats(self) -> Dict[str, any]:
        """Получает глобальную статистику ограничителя"""
        async with self._lock:
            active_users = len(self.user_requests)
            blocked_users = len(self.blocked_users)

            # Подсчитываем общее количество запросов за последнюю минуту
            current_time = time.time()
            minute_ago = current_time - 60
            total_requests = 0

            for user_requests in self.user_requests.values():
                total_requests += sum(1 for req_time in user_requests if req_time > minute_ago)

            return {
                'active_users': active_users,
                'blocked_users': blocked_users,
                'total_requests_last_minute': total_requests,
                'requests_per_minute_limit': self.requests_per_minute
            }

    async def reset_user_limits(self, user_id: int) -> bool:
        """Сбрасывает лимиты для конкретного пользователя (админ функция)"""
        async with self._lock:
            removed_requests = user_id in self.user_requests
            removed_block = user_id in self.blocked_users

            if removed_requests:
                del self.user_requests[user_id]
            if removed_block:
                del self.blocked_users[user_id]

            if removed_requests or removed_block:
                logging.info(f"Rate limits reset for user {user_id}")
                return True
            return False

    async def stop(self):
        """Останавливает ограничитель и очищает ресурсы"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            self.user_requests.clear()
            self.blocked_users.clear()

        logging.info("Rate limiter stopped")


# Глобальный экземпляр ограничителя
rate_limiter = RateLimiter(
    requests_per_minute=getattr(settings, 'USER_RATE_LIMIT_PER_MINUTE', 20)
)


async def check_rate_limit(user_id: int) -> Tuple[bool, Optional[str]]:
    """Удобная функция для проверки лимита запросов"""
    return await rate_limiter.is_allowed(user_id)


async def get_user_rate_stats(user_id: int) -> Dict[str, any]:
    """Получает статистику ограничений для пользователя"""
    return await rate_limiter.get_user_stats(user_id)


async def reset_user_rate_limits(user_id: int) -> bool:
    """Сбрасывает ограничения для пользователя"""
    return await rate_limiter.reset_user_limits(user_id)
