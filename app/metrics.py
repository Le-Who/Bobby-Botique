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
    """Сборщик метрик производительности"""
    
    def __init__(self):
        self.metrics = PerformanceMetrics()
        self.response_times = deque(maxlen=1000)  # Храним последние 1000 запросов
        self.error_log = deque(maxlen=100)  # Храним последние 100 ошибок
        self.daily_metrics: Dict[str, PerformanceMetrics] = defaultdict(PerformanceMetrics)
        self._lock = asyncio.Lock()
    
    async def record_request(self, request_type: str, response_time: float, success: bool = True):
        """Записывает метрики запроса"""
        async with self._lock:
            self.metrics.request_count += 1
            self.metrics.total_response_time += response_time
            self.response_times.append(response_time)
            
            if not success:
                self.metrics.error_count += 1
            
            # Записываем в дневные метрики
            today = date.today().isoformat()
            self.daily_metrics[today].request_count += 1
            self.daily_metrics[today].total_response_time += response_time
            if not success:
                self.daily_metrics[today].error_count += 1
    
    async def record_api_call(self, api_name: str, model: str = None):
        """Записывает вызов API"""
        async with self._lock:
            self.metrics.api_calls[api_name] = self.metrics.api_calls.get(api_name, 0) + 1
            if model:
                self.metrics.model_usage[model] = self.metrics.model_usage.get(model, 0) + 1
            
            today = date.today().isoformat()
            self.daily_metrics[today].api_calls[api_name] = self.daily_metrics[today].api_calls.get(api_name, 0) + 1
            if model:
                self.daily_metrics[today].model_usage[model] = self.daily_metrics[today].model_usage.get(model, 0) + 1
    
    async def record_search_query(self):
        """Записывает поисковый запрос"""
        async with self._lock:
            self.metrics.search_queries += 1
            today = date.today().isoformat()
            self.daily_metrics[today].search_queries += 1
    
    async def record_cache_hit(self):
        """Записывает попадание в кэш"""
        async with self._lock:
            self.metrics.cache_hits += 1
            today = date.today().isoformat()
            self.daily_metrics[today].cache_hits += 1
    
    async def record_cache_miss(self):
        """Записывает промах кэша"""
        async with self._lock:
            self.metrics.cache_misses += 1
            today = date.today().isoformat()
            self.daily_metrics[today].cache_misses += 1
    
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