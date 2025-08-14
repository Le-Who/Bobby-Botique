import hashlib
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import time

from .config import settings
from . import database as db
from .metrics import metrics_collector

@dataclass
class CacheEntry:
    """Запись в кэше"""
    data: Any
    created_at: datetime
    expires_at: datetime
    access_count: int = 0
    last_accessed: datetime = None

class SearchCache:
    """Кэш для результатов поиска"""
    
    def __init__(self):
        self.cache: Dict[str, CacheEntry] = {}
        self.max_size = 1000  # Максимальное количество записей в кэше
        self.default_ttl = 259200  # 3 дня по умолчанию (259200 секунд)
        self.search_ttl = 259200  # 3 дня для поисковых запросов
        self.qna_ttl = 259200  # 3 дня для Q&A запросов
        self._lock = asyncio.Lock()
        self._cleanup_task = None
    
    def _generate_cache_key(self, query: str, search_type: str) -> str:
        """Генерирует ключ кэша для запроса"""
        # Нормализуем запрос (убираем лишние пробелы, приводим к нижнему регистру)
        normalized_query = " ".join(query.lower().split())
        key_data = f"{search_type}:{normalized_query}"
        return hashlib.sha256(key_data.encode()).hexdigest()
    
    def _get_ttl(self, search_type: str) -> int:
        """Возвращает TTL для типа поиска"""
        # Все типы поиска теперь имеют одинаковый TTL - 3 дня
        return self.default_ttl
    
    async def get(self, query: str, search_type: str) -> Optional[Any]:
        """Получает данные из кэша"""
        cache_key = self._generate_cache_key(query, search_type)
        
        async with self._lock:
            entry = self.cache.get(cache_key)
            
            if entry is None:
                await metrics_collector.record_cache_miss()
                return None
            
            # Проверяем, не истек ли срок действия
            if datetime.now() > entry.expires_at:
                del self.cache[cache_key]
                await metrics_collector.record_cache_miss()
                return None
            
            # Обновляем статистику доступа
            entry.access_count += 1
            entry.last_accessed = datetime.now()
            
            await metrics_collector.record_cache_hit()
            # Возвращаем данные с пометкой, что они из кэша
            return {
                'data': entry.data,
                'from_cache': True,
                'cache_key': cache_key,
                'created_at': entry.created_at,
                'expires_at': entry.expires_at
            }
    
    async def set(self, query: str, search_type: str, data: Any):
        """Сохраняет данные в кэш"""
        cache_key = self._generate_cache_key(query, search_type)
        ttl = self._get_ttl(search_type)
        
        async with self._lock:
            # Проверяем размер кэша
            if len(self.cache) >= self.max_size:
                await self._evict_oldest()
            
            now = datetime.now()
            entry = CacheEntry(
                data=data,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl),
                access_count=1,
                last_accessed=now
            )
            
            self.cache[cache_key] = entry
    
    async def _evict_oldest(self):
        """Удаляет самые старые записи из кэша"""
        if not self.cache:
            return
        
        # Находим записи для удаления (20% от размера кэша)
        entries_to_remove = max(1, len(self.cache) // 5)
        
        # Сортируем по времени последнего доступа и количеству обращений
        sorted_entries = sorted(
            self.cache.items(),
            key=lambda x: (x[1].last_accessed or x[1].created_at, -x[1].access_count)
        )
        
        # Удаляем самые старые записи
        for i in range(entries_to_remove):
            if i < len(sorted_entries):
                del self.cache[sorted_entries[i][0]]
    
    async def cleanup_expired(self):
        """Удаляет истекшие записи из кэша"""
        async with self._lock:
            now = datetime.now()
            expired_keys = [
                key for key, entry in self.cache.items()
                if now > entry.expires_at
            ]
            
            for key in expired_keys:
                del self.cache[key]
            
            if expired_keys:
                logging.info(f"Cleaned up {len(expired_keys)} expired cache entries")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику кэша"""
        async with self._lock:
            now = datetime.now()
            total_entries = len(self.cache)
            expired_entries = sum(1 for entry in self.cache.values() if now > entry.expires_at)
            
            if total_entries > 0:
                avg_access_count = sum(entry.access_count for entry in self.cache.values()) / total_entries
            else:
                avg_access_count = 0
            
            return {
                'total_entries': total_entries,
                'expired_entries': expired_entries,
                'max_size': self.max_size,
                'avg_access_count': avg_access_count,
                'cache_hit_rate': metrics_collector.get_cache_hit_rate()
            }
    
    async def clear(self):
        """Очищает весь кэш"""
        async with self._lock:
            self.cache.clear()
            logging.info("Cache cleared")

# Глобальный экземпляр кэша
search_cache = SearchCache()

async def get_cached_search_result(query: str, search_type: str) -> Optional[Dict[str, Any]]:
    """Получает результат поиска из кэша"""
    try:
        result = await search_cache.get(query, search_type)
        if result:
            logging.info(f"Cache hit for query: {query[:50]}...")
            # Возвращаем данные в старом формате для обратной совместимости
            if isinstance(result, dict) and 'data' in result:
                return result['data']
            return result
        return None
    except Exception as e:
        logging.error(f"Error getting from cache: {e}")
        return None

async def get_cached_search_result_with_metadata(query: str, search_type: str) -> Optional[Dict[str, Any]]:
    """Получает результат поиска из кэша с метаданными (включая информацию о кэше)"""
    try:
        result = await search_cache.get(query, search_type)
        if result:
            logging.info(f"Cache hit for query: {query[:50]}...")
        return result
    except Exception as e:
        logging.error(f"Error getting from cache: {e}")
        return None

async def cache_search_result(query: str, search_type: str, result: Dict[str, Any]):
    """Сохраняет результат поиска в кэш"""
    try:
        await search_cache.set(query, search_type, result)
        logging.info(f"Cached search result for query: {query[:50]}...")
    except Exception as e:
        logging.error(f"Error caching result: {e}")

async def start_cache_cleanup_task():
    """Запускает задачу очистки кэша"""
    async def cleanup_loop():
        while True:
            try:
                await search_cache.cleanup_expired()
                await asyncio.sleep(300)  # Проверяем каждые 5 минут
            except Exception as e:
                logging.error(f"Error in cache cleanup: {e}")
                await asyncio.sleep(60)
    
    asyncio.create_task(cleanup_loop()) 