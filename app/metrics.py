import time
import logging
import asyncio
from datetime import datetime, date
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from collections import defaultdict, deque
import json

from app.config import settings
from app import database as db

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
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
                ON CONFLICT (metric_date) DO UPDATE SET
                    request_count = metrics.request_count + $10,
                    total_response_time = metrics.total_response_time + $11,
                    error_count = metrics.error_count + $12,
                    search_queries = metrics.search_queries + $13,
                    cache_hits = metrics.cache_hits + $14,
                    cache_misses = metrics.cache_misses + $15,
                    api_calls = COALESCE(metrics.api_calls, '{}'::jsonb) || $16::jsonb,
                    model_usage = COALESCE(metrics.model_usage, '{}'::jsonb) || $17::jsonb,
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
            
            # Сохраняем новые ошибки (только те, которые еще не были сохранены)
            new_errors = [error for error in self.error_log if not error.get('saved', False)]
            if new_errors:
                for error in new_errors:
                    await db.db_query("""
                        INSERT INTO error_logs (error_type, error_message)
                        VALUES ($1, $2)
                    """, (error['type'], error['message']))
                    error['saved'] = True  # Помечаем как сохраненную
            
            self._last_save_time = time.time()
            logging.info(f"Metrics saved to database: {daily_metrics.request_count} requests, {daily_metrics.error_count} errors")
            
        except Exception as e:
            logging.error(f"Error saving metrics to database: {e}")
    
    async def _load_metrics_from_db(self):
        """Загружает метрики из базы данных"""
        try:
            await self._ensure_metrics_tables()
            
            # Загружаем общие метрики (без JSONB полей в основном запросе)
            result = await db.db_query("""
                SELECT 
                    COALESCE(SUM(request_count), 0) as total_requests,
                    COALESCE(SUM(total_response_time), 0.0) as total_time,
                    COALESCE(SUM(error_count), 0) as total_errors,
                    COALESCE(SUM(search_queries), 0) as total_searches,
                    COALESCE(SUM(cache_hits), 0) as total_cache_hits,
                    COALESCE(SUM(cache_misses), 0) as total_cache_misses
                FROM metrics
                WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
            """)
            
            if result and result[0]:
                row = result[0]
                self.metrics.request_count = row['total_requests']
                self.metrics.total_response_time = row['total_time']
                self.metrics.error_count = row['total_errors']
                self.metrics.search_queries = row['total_searches']
                self.metrics.cache_hits = row['total_cache_hits']
                self.metrics.cache_misses = row['total_cache_misses']
            
            # Отдельно загружаем и объединяем JSONB поля
            jsonb_result = await db.db_query("""
                SELECT api_calls, model_usage
                FROM metrics
                WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
                AND (api_calls IS NOT NULL OR model_usage IS NOT NULL)
            """)
            
            # Объединяем JSONB данные
            combined_api_calls = {}
            combined_model_usage = {}
            
            for row in jsonb_result:
                # Обрабатываем api_calls
                if row['api_calls']:
                    if isinstance(row['api_calls'], dict):
                        for key, value in row['api_calls'].items():
                            combined_api_calls[key] = combined_api_calls.get(key, 0) + value
                    elif isinstance(row['api_calls'], str):
                        try:
                            api_calls_dict = json.loads(row['api_calls'])
                            for key, value in api_calls_dict.items():
                                combined_api_calls[key] = combined_api_calls.get(key, 0) + value
                        except:
                            pass
                
                # Обрабатываем model_usage
                if row['model_usage']:
                    if isinstance(row['model_usage'], dict):
                        for key, value in row['model_usage'].items():
                            combined_model_usage[key] = combined_model_usage.get(key, 0) + value
                    elif isinstance(row['model_usage'], str):
                        try:
                            model_usage_dict = json.loads(row['model_usage'])
                            for key, value in model_usage_dict.items():
                                combined_model_usage[key] = combined_model_usage.get(key, 0) + value
                        except:
                            pass
            
            self.metrics.api_calls = combined_api_calls
            self.metrics.model_usage = combined_model_usage
            
            # Загружаем дневные метрики за последние 7 дней
            daily_result = await db.db_query("""
                SELECT metric_date, request_count, total_response_time, error_count,
                       search_queries, cache_hits, cache_misses, api_calls, model_usage
                FROM metrics
                WHERE metric_date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY metric_date DESC
            """)
            
            for row in daily_result:
                try:
                    date_str = row['metric_date'].isoformat()
                    self.daily_metrics[date_str] = PerformanceMetrics(
                        request_count=row.get('request_count', 0) or 0,
                        total_response_time=row.get('total_response_time', 0.0) or 0.0,
                        error_count=row.get('error_count', 0) or 0,
                        search_queries=row.get('search_queries', 0) or 0,
                        cache_hits=row.get('cache_hits', 0) or 0,
                        cache_misses=row.get('cache_misses', 0) or 0,
                        api_calls=dict(row['api_calls']) if row.get('api_calls') and isinstance(row['api_calls'], dict) else {},
                        model_usage=dict(row['model_usage']) if row.get('model_usage') and isinstance(row['model_usage'], dict) else {}
                    )
                except Exception as e:
                    logging.warning(f"Failed to process daily metrics row: {e}, row: {row}")
                    continue
            
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
            today = date.today().isoformat()
            self.daily_metrics[today].request_count += 1
            self.daily_metrics[today].total_response_time += response_time
            if not success:
                self.daily_metrics[today].error_count += 1
            
            # Периодически сохраняем в БД
            if time.time() - self._last_save_time > self._save_interval:
                await self._save_metrics_to_db()
    
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
            # Добавляем в локальный лог
            self.error_log.append({
                'timestamp': datetime.now().isoformat(),
                'type': error_type,
                'message': error_message,
                'saved': False  # Флаг для отслеживания сохранения
            })
            
            # Сохраняем в базу данных
            try:
                await self._ensure_metrics_tables()
                await db.db_query(
                    "INSERT INTO error_logs (error_type, error_message) VALUES ($1, $2)",
                    (error_type, error_message)
                )
                # Отмечаем как сохраненную
                if self.error_log:
                    self.error_log[-1]['saved'] = True
            except Exception as e:
                logging.error(f"Failed to save error to database: {e}")
    
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
            
            # Загружаем последние ошибки из базы данных
            try:
                recent_errors_result = await db.db_query("""
                    SELECT error_type, error_message, created_at
                    FROM error_logs
                    ORDER BY created_at DESC
                    LIMIT 10
                """)
                recent_errors = [
                    {
                        'type': row['error_type'],
                        'message': row['error_message'],
                        'timestamp': row['created_at'].isoformat() if row['created_at'] else None
                    }
                    for row in recent_errors_result
                ]
            except Exception as e:
                logging.error(f"Error loading recent errors: {e}")
                recent_errors = list(self.error_log)[-10:]  # Fallback на локальный лог
            
            summary = {
                'total_requests': self.metrics.request_count,
                'average_response_time': self.get_average_response_time(),
                'error_rate': self.get_error_rate(),
                'cache_hit_rate': self.get_cache_hit_rate(),
                'api_calls': dict(self.metrics.api_calls),
                'model_usage': dict(self.metrics.model_usage),
                'search_queries': self.metrics.search_queries,
                'recent_errors': recent_errors,
                'daily_metrics': {
                    date: {
                        'requests': metrics.request_count,
                        'errors': metrics.error_count,
                        'avg_response_time': metrics.total_response_time / metrics.request_count if metrics.request_count > 0 else 0
                    }
                    for date, metrics in self.daily_metrics.items()
                }
            }
            
            logging.info(f"Metrics summary: {summary['total_requests']} requests, {summary['error_rate']:.1f}% errors")
            return summary
    
    async def initialize(self):
        """Инициализирует систему метрик"""
        await self._load_metrics_from_db()
    
    async def cleanup(self):
        """Очищает ресурсы и сохраняет метрики"""
        try:
            # Проверяем, что база данных доступна перед сохранением
            if db.db_pool and not db.db_pool._closed:
                await self._save_metrics_to_db()
            else:
                logging.warning("Database pool unavailable during metrics cleanup, skipping save")
        except Exception as e:
            logging.error(f"Error during metrics cleanup: {e}")
            # Не позволяем ошибкам метрик прерывать shutdown

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

# ============================================================================
# ROLE AND CONVERSATION METRICS
# ============================================================================

@dataclass
class RoleMetrics:
    """Метрики использования ролей"""
    role_applications: Dict[str, int] = field(default_factory=dict)  # role_key -> count
    custom_roles_created: int = 0
    role_clears: int = 0
    role_saves: int = 0

@dataclass
class ConversationMetrics:
    """Метрики работы с беседами"""
    conversations_saved: int = 0
    conversations_switched: int = 0
    conversations_renamed: int = 0
    conversations_deleted: int = 0
    total_conversations: int = 0

@dataclass
class SummarizationMetrics:
    """Метрики суммаризации"""
    summarizations_triggered: int = 0
    summarizations_soft_limit: int = 0
    summarizations_hard_limit: int = 0
    total_tokens_saved: int = 0
    average_summary_length: float = 0.0

class RoleConversationMetricsCollector:
    """Сборщик метрик для ролей и бесед"""
    
    def __init__(self):
        self.role_metrics = RoleMetrics()
        self.conversation_metrics = ConversationMetrics()
        self.summarization_metrics = SummarizationMetrics()
        self._lock = asyncio.Lock()
    
    async def record_role_application(self, role_key: str):
        """Записывает применение роли"""
        async with self._lock:
            self.role_metrics.role_applications[role_key] = self.role_metrics.role_applications.get(role_key, 0) + 1
            logging.info(f"Role applied: {role_key}")
    
    async def record_custom_role_creation(self):
        """Записывает создание кастомной роли"""
        async with self._lock:
            self.role_metrics.custom_roles_created += 1
            logging.info("Custom role created")
    
    async def record_role_clear(self):
        """Записывает сброс роли"""
        async with self._lock:
            self.role_metrics.role_clears += 1
            logging.info("Role cleared")
    
    async def record_role_save(self):
        """Записывает сохранение роли"""
        async with self._lock:
            self.role_metrics.role_saves += 1
            logging.info("Role saved")
    
    async def record_conversation_saved(self):
        """Записывает сохранение беседы"""
        async with self._lock:
            self.conversation_metrics.conversations_saved += 1
            logging.info("Conversation saved")
    
    async def record_conversation_switched(self):
        """Записывает переключение на беседу"""
        async with self._lock:
            self.conversation_metrics.conversations_switched += 1
            logging.info("Conversation switched")
    
    async def record_conversation_renamed(self):
        """Записывает переименование беседы"""
        async with self._lock:
            self.conversation_metrics.conversations_renamed += 1
            logging.info("Conversation renamed")
    
    async def record_conversation_deleted(self):
        """Записывает удаление беседы"""
        async with self._lock:
            self.conversation_metrics.conversations_deleted += 1
            logging.info("Conversation deleted")
    
    async def record_summarization(self, reason: str, tokens_saved: int, summary_length: int):
        """Записывает суммаризацию контекста"""
        async with self._lock:
            self.summarization_metrics.summarizations_triggered += 1
            
            if "мягкий лимит" in reason:
                self.summarization_metrics.summarizations_soft_limit += 1
            elif "жёсткий лимит" in reason:
                self.summarization_metrics.summarizations_hard_limit += 1
            
            self.summarization_metrics.total_tokens_saved += tokens_saved
            
            # Обновляем среднюю длину суммаризации
            current_avg = self.summarization_metrics.average_summary_length
            count = self.summarization_metrics.summarizations_triggered
            self.summarization_metrics.average_summary_length = (current_avg * (count - 1) + summary_length) / count
            
            logging.info(f"Summarization triggered: {reason}, tokens saved: {tokens_saved}")
    
    async def get_metrics_summary(self) -> Dict[str, Any]:
        """Возвращает сводку метрик"""
        async with self._lock:
            return {
                'roles': {
                    'applications': dict(self.role_metrics.role_applications),
                    'custom_created': self.role_metrics.custom_roles_created,
                    'clears': self.role_metrics.role_clears,
                    'saves': self.role_metrics.role_saves
                },
                'conversations': {
                    'saved': self.conversation_metrics.conversations_saved,
                    'switched': self.conversation_metrics.conversations_switched,
                    'renamed': self.conversation_metrics.conversations_renamed,
                    'deleted': self.conversation_metrics.conversations_deleted,
                    'total': self.conversation_metrics.total_conversations
                },
                'summarization': {
                    'triggered': self.summarization_metrics.summarizations_triggered,
                    'soft_limit': self.summarization_metrics.summarizations_soft_limit,
                    'hard_limit': self.summarization_metrics.summarizations_hard_limit,
                    'tokens_saved': self.summarization_metrics.total_tokens_saved,
                    'avg_summary_length': self.summarization_metrics.average_summary_length
                }
            }

# Глобальный экземпляр сборщика метрик ролей и бесед
role_conv_metrics = RoleConversationMetricsCollector() 