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
        cache_key = hashlib.sha256(key_data.encode()).hexdigest()
        logging.debug(f"Generated cache key: {cache_key[:16]}... for query: '{query[:30]}...' (type: {search_type})")
        return cache_key
    
    def _get_ttl(self, search_type: str) -> int:
        """Возвращает TTL для типа поиска"""
        # Все типы поиска теперь имеют одинаковый TTL - 3 дня
        ttl = self.default_ttl
        logging.debug(f"TTL for search type '{search_type}': {ttl}s ({ttl/86400:.1f} days)")
        return ttl
    
    async def get(self, query: str, search_type: str) -> Optional[Any]:
        """Получает данные из кэша"""
        cache_key = self._generate_cache_key(query, search_type)
        
        async with self._lock:
            entry = self.cache.get(cache_key)
            
            if entry is None:
                logging.debug(f"Cache entry not found for key: {cache_key[:16]}... (query: {query[:30]}..., type: {search_type})")
                await metrics_collector.record_cache_miss()
                return None
            
            # Проверяем, не истек ли срок действия
            if datetime.now() > entry.expires_at:
                logging.debug(f"Cache entry expired for key: {cache_key[:16]}... (query: {query[:30]}..., type: {search_type})")
                del self.cache[cache_key]
                await metrics_collector.record_cache_miss()
                return None
            
            # Обновляем статистику доступа
            entry.access_count += 1
            entry.last_accessed = datetime.now()
            
            logging.debug(f"Cache hit for key: {cache_key[:16]}... (query: {query[:30]}..., type: {search_type})")
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
                logging.debug(f"Cache size limit reached ({len(self.cache)}), evicting oldest entries")
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
            logging.debug(f"Cache entry saved for key: {cache_key[:16]}... (query: {query[:30]}..., type: {search_type}, ttl: {ttl}s)")
    
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
            
            # Очищаем соответствующие button_id_mapping
            if expired_keys:
                button_ids_to_remove = [bid for bid, (_, _, ck) in button_id_mapping.items() if ck in expired_keys]
                for bid in button_ids_to_remove:
                    del button_id_mapping[bid]
                if button_ids_to_remove:
                    logging.debug(f"Cleaned up {len(button_ids_to_remove)} button mappings for expired cache entries")
            
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
            # Очищаем все button_id_mapping
            button_id_mapping.clear()
            logging.info("Cache and button mappings cleared")
    
    async def _remove_by_key(self, cache_key: str):
        """Удаляет запись из кэша по ключу"""
        async with self._lock:
            if cache_key in self.cache:
                del self.cache[cache_key]
                logging.info(f"Removed cache entry with key: {cache_key[:16]}...")
                
                # Очищаем соответствующие button_id_mapping
                button_ids_to_remove = [bid for bid, (_, _, ck) in button_id_mapping.items() if ck == cache_key]
                for bid in button_ids_to_remove:
                    del button_id_mapping[bid]
                if button_ids_to_remove:
                    logging.debug(f"Cleaned up {len(button_ids_to_remove)} button mappings for removed cache entry")
                
                return True
            return False

# Глобальный экземпляр кэша
search_cache = SearchCache()

# Словарь для хранения соответствия между button_id и данными кэша
# button_id -> (query, search_type, cache_key)
button_id_mapping = {}

def _generate_button_id(cache_key: str) -> str:
    """Генерирует уникальный ID для кнопки актуализации"""
    import hashlib
    return hashlib.md5(cache_key.encode()).hexdigest()[:16]

def _store_button_mapping(button_id: str, query: str, search_type: str, cache_key: str):
    """Сохраняет соответствие между button_id и данными кэша"""
    # Очищаем старые записи, если их слишком много
    if len(button_id_mapping) > 1000:
        # Удаляем 20% самых старых записей
        keys_to_remove = list(button_id_mapping.keys())[:200]
        for key in keys_to_remove:
            del button_id_mapping[key]
        logging.info(f"Cleaned up {len(keys_to_remove)} old button mappings")
    
    button_id_mapping[button_id] = (query, search_type, cache_key)

def _get_button_mapping(button_id: str):
    """Получает данные кэша по button_id"""
    return button_id_mapping.get(button_id)

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
            logging.info(f"Cache hit for query: {query[:50]}... (type: {search_type})")
            logging.debug(f"Cache result keys: {result.keys() if isinstance(result, dict) else 'not a dict'}")
        else:
            logging.info(f"Cache miss for query: {query[:50]}... (type: {search_type})")
        return result
    except Exception as e:
        logging.error(f"Error getting from cache: {e}")
        return None

async def cache_search_result(query: str, search_type: str, result: Dict[str, Any]):
    """Сохраняет результат поиска в кэш"""
    try:
        await search_cache.set(query, search_type, result)
        logging.info(f"Cached search result for query: {query[:50]}... (type: {search_type})")
        logging.debug(f"Cached result keys: {result.keys() if isinstance(result, dict) else 'not a dict'}")
        
        # Генерируем button_id и сохраняем соответствие
        cache_key = search_cache._generate_cache_key(query, search_type)
        button_id = _generate_button_id(cache_key)
        _store_button_mapping(button_id, query, search_type, cache_key)
        logging.debug(f"Stored button mapping: {button_id} -> {query[:30]}... ({search_type})")
        
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