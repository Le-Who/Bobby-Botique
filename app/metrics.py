import time
import logging
import asyncio
from datetime import datetime, date
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from collections import defaultdict, deque
import json

from .config import settings
from . import database as db

@dataclass
class PerformanceMetrics:
    """Класс для хранения метрик производительности"""
    request_count: int = 0
    total_response_time: float = 0.0
    error_count: int = 0
    api_calls: Dict[str, int] = field(default_factory=dict)
    model_usage: Dict[str, int] = field(default_factory=dict)
    search_queries: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

class MetricsCollector:
    """Сборщик метрик производительности с поддержкой базы данных"""
    
    def __init__(self):
        self.metrics = PerformanceMetrics()
        self.response_times = deque(maxlen=1000)  # Храним последние 1000 запросов
        self.error_log = deque(maxlen=100)  # Храним последние 100 ошибок
        self.daily_metrics: Dict[str, PerformanceMetrics] = defaultdict(PerformanceMetrics)
        self._lock = asyncio.Lock()
        self._last_save_time = time.time()
        self._save_interval = 300  # Сохраняем каждые 5 минут
    
    async def _ensure_metrics_tables(self):
        """Создает таблицы для метрик, если они не существуют"""
        try:
            # Таблица для общих метрик
            await db.db_query("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    metric_date DATE NOT NULL,
                    request_count INTEGER DEFAULT 0,
                    total_response_time REAL DEFAULT 0.0,
                    error_count INTEGER DEFAULT 0,
                    search_queries INTEGER DEFAULT 0,
                    cache_hits INTEGER DEFAULT 0,
                    cache_misses INTEGER DEFAULT 0,
                    api_calls JSONB DEFAULT '{}',
                    model_usage JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(metric_date)
                )
            """)
            
            # Таблица для ошибок
            await db.db_query("""
                CREATE TABLE IF NOT EXISTS error_logs (
                    id SERIAL PRIMARY KEY,
                    error_type TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            logging.info("Metrics tables ensured")
        except Exception as e:
            logging.error(f"Error creating metrics tables: {e}")
    
    async def _save_metrics_to_db(self):
        """Сохраняет текущие метрики в базу данных"""
        try:
            await self._ensure_metrics_tables()
            
            today = date.today()
            today_str = today.isoformat()
            daily_metrics = self.daily_metrics.get(today_str, PerformanceMetrics())
            
            # Обновляем или вставляем метрики за сегодня
            await db.db_query("""
                INSERT INTO metrics (metric_date, request_count, total_response_time, error_count, 
                                   search_queries, cache_hits, cache_misses, api_calls, model_usage, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (metric_date) DO UPDATE SET
                    request_count = metrics.request_count + ?,
                    total_response_time = metrics.total_response_time + ?,
                    error_count = metrics.error_count + ?,
                    search_queries = metrics.search_queries + ?,
                    cache_hits = metrics.cache_hits + ?,
                    cache_misses = metrics.cache_misses + ?,
                    api_calls = metrics.api_calls || ?::jsonb,
                    model_usage = metrics.model_usage || ?::jsonb,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                today, daily_metrics.request_count, daily_metrics.total_response_time, 
                daily_metrics.error_count, daily_metrics.search_queries, daily_metrics.cache_hits, 
                daily_metrics.cache_misses, json.dumps(daily_metrics.api_calls), 
                json.dumps(daily_metrics.model_usage),
                # Значения для UPDATE
                daily_metrics.request_count, daily_metrics.total_response_time, 
                daily_metrics.error_count, daily_metrics.search_queries, daily_metrics.cache_hits, 
                daily_metrics.cache_misses, json.dumps(daily_metrics.api_calls), 
                json.dumps(daily_metrics.model_usage)
            ))
            
            # Сохраняем новые ошибки
            if self.error_log:
                for error in list(self.error_log):
                    await db.db_query("""
                        INSERT INTO error_logs (error_type, error_message)
                        VALUES (?, ?)
                    """, (error['type'], error['message']))
                
                # Очищаем сохраненные ошибки
                self.error_log.clear()
            
            self._last_save_time = time.time()
            logging.debug("Metrics saved to database")
            
        except Exception as e:
            logging.error(f"Error saving metrics to database: {e}")
    
    async def _load_metrics_from_db(self):
        """Загружает метрики из базы данных"""
        try:
            await self._ensure_metrics_tables()
            
            # Загружаем общие метрики
            result = await db.db_query("""
                SELECT 
                    SUM(request_count) as total_requests,
                    SUM(total_response_time) as total_time,
                    SUM(error_count) as total_errors,
                    SUM(search_queries) as total_searches,
                    SUM(cache_hits) as total_cache_hits,
                    SUM(cache_misses) as total_cache_misses,
                    api_calls,
                    model_usage
                FROM metrics
                WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
            """)
            
            if result and result[0]:
                row = result[0]
                self.metrics.request_count = row['total_requests'] or 0
                self.metrics.total_response_time = row['total_time'] or 0.0
                self.metrics.error_count = row['total_errors'] or 0
                self.metrics.search_queries = row['total_searches'] or 0
                self.metrics.cache_hits = row['total_cache_hits'] or 0
                self.metrics.cache_misses = row['total_cache_misses'] or 0
                
                if row['api_calls']:
                    self.metrics.api_calls = row['api_calls']
                if row['model_usage']:
                    self.metrics.model_usage = row['model_usage']
            
            # Загружаем дневные метрики за последние 7 дней
            daily_result = await db.db_query("""
                SELECT metric_date, request_count, total_response_time, error_count,
                       search_queries, cache_hits, cache_misses, api_calls, model_usage
                FROM metrics
                WHERE metric_date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY metric_date DESC
            """)
            
            for row in daily_result:
                date_str = row['metric_date'].isoformat()
                self.daily_metrics[date_str] = PerformanceMetrics(
                    request_count=row['request_count'],
                    total_response_time=row['total_response_time'],
                    error_count=row['error_count'],
                    search_queries=row['search_queries'],
                    cache_hits=row['cache_hits'],
                    cache_misses=row['cache_misses'],
                    api_calls=row['api_calls'] if row['api_calls'] else {},
                    model_usage=row['model_usage'] if row['model_usage'] else {}
                )
            
            # Загружаем последние ошибки
            error_result = await db.db_query("""
                SELECT error_type, error_message, created_at
                FROM error_logs
                ORDER BY created_at DESC
                LIMIT 100
            """)
            
            for row in error_result:
                self.error_log.append({
                    'timestamp': row['created_at'].isoformat(),
                    'type': row['error_type'],
                    'message': row['error_message']
                })
            
            logging.info("Metrics loaded from database")
            
        except Exception as e:
            logging.error(f"Error loading metrics from database: {e}")
    
    async def record_request(self, request_type: str, response_time: float, success: bool = True):
        """Записывает метрики запроса"""
        async with self._lock:
            self.metrics.request_count += 1
            self.metrics.total_response_time += response_time
            self.response_times.append(response_time)
            
            if not success:
                self.metrics.error_count += 1
            
            # Записываем в дневные метрики
            today_str = date.today().isoformat()
            self.daily_metrics[today_str].request_count += 1
            self.daily_metrics[today_str].total_response_time += response_time
            if not success:
                self.daily_metrics[today_str].error_count += 1
            
            # Периодически сохраняем в БД
            if time.time() - self._last_save_time > self._save_interval:
                await self._save_metrics_to_db()
    
    async def record_api_call(self, api_name: str, model: str = None):
        """Записывает вызов API"""
        async with self._lock:
            self.metrics.api_calls[api_name] = self.metrics.api_calls.get(api_name, 0) + 1
            if model:
                self.metrics.model_usage[model] = self.metrics.model_usage.get(model, 0) + 1
            
            today_str = date.today().isoformat()
            self.daily_metrics[today_str].api_calls[api_name] = self.daily_metrics[today_str].api_calls.get(api_name, 0) + 1
            if model:
                self.daily_metrics[today_str].model_usage[model] = self.daily_metrics[today_str].model_usage.get(model, 0) + 1
    
    async def record_search_query(self):
        """Записывает поисковый запрос"""
        async with self._lock:
            self.metrics.search_queries += 1
            today_str = date.today().isoformat()
            self.daily_metrics[today_str].search_queries += 1
    
    async def record_cache_hit(self):
        """Записывает попадание в кэш"""
        async with self._lock:
            self.metrics.cache_hits += 1
            today_str = date.today().isoformat()
            self.daily_metrics[today_str].cache_hits += 1
    
    async def record_cache_miss(self):
        """Записывает промах кэша"""
        async with self._lock:
            self.metrics.cache_misses += 1
            today_str = date.today().isoformat()
            self.daily_metrics[today_str].cache_misses += 1
    
    async def record_error(self, error_type: str, error_message: str):
        """Записывает ошибку"""
        async with self._lock:
            self.error_log.append({
                'timestamp': datetime.now().isoformat(),
                'type': error_type,
                'message': error_message
            })
    
    def get_average_response_time(self) -> float:
        """Возвращает среднее время ответа"""
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)
    
    def get_error_rate(self) -> float:
        """Возвращает процент ошибок"""
        if self.metrics.request_count == 0:
            return 0.0
        return (self.metrics.error_count / self.metrics.request_count) * 100
    
    def get_cache_hit_rate(self) -> float:
        """Возвращает процент попаданий в кэш"""
        total_cache_requests = self.metrics.cache_hits + self.metrics.cache_misses
        if total_cache_requests == 0:
            return 0.0
        return (self.metrics.cache_hits / total_cache_requests) * 100
    
    async def get_metrics_summary(self) -> Dict[str, Any]:
        """Возвращает сводку метрик"""
        async with self._lock:
            # Принудительно сохраняем текущие метрики перед получением сводки
            await self._save_metrics_to_db()
            
            return {
                'total_requests': self.metrics.request_count,
                'average_response_time': self.get_average_response_time(),
                'error_rate': self.get_error_rate(),
                'cache_hit_rate': self.get_cache_hit_rate(),
                'api_calls': dict(self.metrics.api_calls),
                'model_usage': dict(self.metrics.model_usage),
                'search_queries': self.metrics.search_queries,
                'recent_errors': list(self.error_log)[-10:],  # Последние 10 ошибок
                'daily_metrics': {
                    date: {
                        'requests': metrics.request_count,
                        'errors': metrics.error_count,
                        'avg_response_time': metrics.total_response_time / metrics.request_count if metrics.request_count > 0 else 0
                    }
                    for date, metrics in self.daily_metrics.items()
                }
            }
    
    async def initialize(self):
        """Инициализирует систему метрик"""
        await self._load_metrics_from_db()
    
    async def cleanup(self):
        """Очищает ресурсы и сохраняет метрики"""
        await self._save_metrics_to_db()

# Глобальный экземпляр сборщика метрик
metrics_collector = MetricsCollector()

class MetricsMiddleware:
    """Middleware для автоматического сбора метрик"""
    
    def __init__(self, func_name: str):
        self.func_name = func_name
    
    async def __aenter__(self):
        self.start_time = time.time()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        response_time = time.time() - self.start_time
        success = exc_type is None
        
        await metrics_collector.record_request(self.func_name, response_time, success)
        
        if not success:
            await metrics_collector.record_error(
                exc_type.__name__ if exc_type else 'Unknown',
                str(exc_val) if exc_val else 'Unknown error'
            )

def track_metrics(func_name: str):
    """Декоратор для отслеживания метрик функции"""
    async def decorator(func):
        async def wrapper(*args, **kwargs):
            async with MetricsMiddleware(func_name):
                return await func(*args, **kwargs)
        return wrapper
    return decorator 