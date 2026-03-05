import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app import database as db
from app.request_context import get_request_id
from app.utils import time as time_utils


@dataclass
class PerformanceMetrics:
    """Класс для хранения метрик производительности"""

    request_count: int = 0
    total_response_time: float = 0.0
    error_count: int = 0
    api_calls: dict[str, int] = field(default_factory=dict)
    model_usage: dict[str, int] = field(default_factory=dict)
    search_queries: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


class MetricsCollector:
    """Сборщик метрик производительности с поддержкой базы данных"""

    def __init__(self):
        self.metrics = PerformanceMetrics()
        self.response_times = deque(maxlen=1000)  # Храним afterдние 1000 requestов
        self.error_log = deque(maxlen=100)  # Храним afterдние 100 ошибок
        self.api_event_log = deque(maxlen=200)  # Храним afterдние API события
        self.daily_metrics: dict[str, PerformanceMetrics] = defaultdict(PerformanceMetrics)
        # Per-user daily metrics: key = (date_str, user_id)
        self._user_daily: dict[tuple, dict[str, Any]] = defaultdict(lambda: {"request_count": 0, "model_usage": {}})
        self._events_queue = asyncio.Queue()
        self._last_save_time = time.time()
        self._save_interval = 300  # Save каждые 5 минут
        self._bg_save_task = None

    async def _event_processor(self):
        """Background task to process events and periodically save metrics"""
        logging.info("Metrics background event processor started")
        last_save = time.time()
        while True:
            try:
                timeout = max(0.1, self._save_interval - (time.time() - last_save))
                try:
                    event = await asyncio.wait_for(self._events_queue.get(), timeout=timeout)
                    self._process_event(event)
                    self._events_queue.task_done()
                except TimeoutError:
                    pass

                now = time.time()
                if now - last_save >= self._save_interval:
                    if db.db_manager.is_connected:
                        await self._save_metrics_to_db()
                    self._prune_old_metrics()
                    last_save = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Error in metrics event processor: %s", e, exc_info=True)
                await asyncio.sleep(5)

    def _prune_old_metrics(self):
        """Remove stale daily_metrics and _user_daily entries to prevent unbounded growth."""
        today = date.today()
        # Prune daily_metrics older than 8 days
        from datetime import timedelta as _td

        cutoff = (today - _td(days=8)).isoformat()
        stale_days = [d for d in self.daily_metrics if d < cutoff]
        for d in stale_days:
            del self.daily_metrics[d]
        # Prune _user_daily for past days (keep only today)
        today_str = today.isoformat()
        stale_user_keys = [k for k in self._user_daily if k[0] != today_str]
        for k in stale_user_keys:
            del self._user_daily[k]
        if stale_days or stale_user_keys:
            logging.debug(
                "Metrics pruned: %d stale days, %d stale user-day entries",
                len(stale_days),
                len(stale_user_keys),
            )

    def _process_event(self, event: dict[str, Any]):
        """Обрабатывает одно событие метрики и обновляет локальные словари без блокировок"""
        today = date.today().isoformat()
        event_type = event.get("type")

        if event_type == "request":
            response_time = event["response_time"]
            success = event["success"]

            self.metrics.request_count += 1
            self.metrics.total_response_time += response_time
            self.response_times.append(response_time)

            if not success:
                self.metrics.error_count += 1
                self.daily_metrics[today].error_count += 1

            self.daily_metrics[today].request_count += 1
            self.daily_metrics[today].total_response_time += response_time

            # Per-user tracking
            uid = event.get("user_id")
            if uid:
                ukey = (today, uid)
                self._user_daily[ukey]["request_count"] += 1

        elif event_type == "api_call":
            api_name = event["api_name"]
            model = event["model"]

            self.metrics.api_calls[api_name] = self.metrics.api_calls.get(api_name, 0) + 1
            self.daily_metrics[today].api_calls[api_name] = self.daily_metrics[today].api_calls.get(api_name, 0) + 1

            if model:
                self.metrics.model_usage[model] = self.metrics.model_usage.get(model, 0) + 1
                self.daily_metrics[today].model_usage[model] = self.daily_metrics[today].model_usage.get(model, 0) + 1

                # Per-user model usage
                uid = event.get("user_id")
                if uid:
                    ukey = (today, uid)
                    mu = self._user_daily[ukey]["model_usage"]
                    mu[model] = mu.get(model, 0) + 1

            self.api_event_log.append(
                {
                    "timestamp": event["timestamp"],
                    "api": api_name,
                    "model": model,
                    "request_id": event["request_id"],
                }
            )

        elif event_type == "search_query":
            self.metrics.search_queries += 1
            self.daily_metrics[today].search_queries += 1

        elif event_type == "cache_hit":
            self.metrics.cache_hits += 1
            self.daily_metrics[today].cache_hits += 1

        elif event_type == "cache_miss":
            self.metrics.cache_misses += 1
            self.daily_metrics[today].cache_misses += 1

        elif event_type == "error":
            self.error_log.append(
                {
                    "timestamp": event["timestamp"],
                    "type": event["error_type"],
                    "message": event["error_message"],
                    "request_id": event["request_id"],
                    "saved": False,
                }
            )

    async def _save_metrics_to_db(self):
        """Сохраняет текущие метрики в базу данных (Non-blocking)"""
        # Phase 1: Snapshot data under lock (Fast)
        snapshot_data = None

        # Phase 1: Snapshot data
        try:
            today = date.today()
            today_str = today.isoformat()

            # Snapshot daily metrics
            daily = self.daily_metrics.get(today_str, PerformanceMetrics())

            # Deep copy dicts to avoid concurrent modification issues during JSON serialization
            api_calls_copy = dict(daily.api_calls)
            model_usage_copy = dict(daily.model_usage)

            snapshot_data = {
                "date": today,
                "request_count": daily.request_count,
                "total_response_time": daily.total_response_time,
                "error_count": daily.error_count,
                "search_queries": daily.search_queries,
                "cache_hits": daily.cache_hits,
                "cache_misses": daily.cache_misses,
                "api_calls": api_calls_copy,
                "model_usage": model_usage_copy,
            }

            # Snapshot unsaved errors
            errors_to_process = [error for error in self.error_log if not error.get("saved", False)]

        except Exception as e:
            logging.error("Error creating metrics snapshot: %s", e, exc_info=True)
            return

        # Phase 2: Save to DB (IO - No Lock)
        try:
            if snapshot_data:
                # Update or вставляем metrics за сегодня
                # Using SET (upsert replacement) to handle updates correctly
                await db.db_query(
                    """
                    INSERT INTO metrics (metric_date, request_count, total_response_time, error_count,
                                       search_queries, cache_hits, cache_misses, api_calls, model_usage, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
                    ON CONFLICT (metric_date) DO UPDATE SET
                        request_count = EXCLUDED.request_count,
                        total_response_time = EXCLUDED.total_response_time,
                        error_count = EXCLUDED.error_count,
                        search_queries = EXCLUDED.search_queries,
                        cache_hits = EXCLUDED.cache_hits,
                        cache_misses = EXCLUDED.cache_misses,
                        api_calls = COALESCE(metrics.api_calls, '{}'::jsonb) || EXCLUDED.api_calls,
                        model_usage = COALESCE(metrics.model_usage, '{}'::jsonb) || EXCLUDED.model_usage,
                        updated_at = CURRENT_TIMESTAMP
                """,
                    (
                        snapshot_data["date"],
                        snapshot_data["request_count"],
                        snapshot_data["total_response_time"],
                        snapshot_data["error_count"],
                        snapshot_data["search_queries"],
                        snapshot_data["cache_hits"],
                        snapshot_data["cache_misses"],
                        json.dumps(snapshot_data["api_calls"]),
                        json.dumps(snapshot_data["model_usage"]),
                    ),
                )

            # Save new errors
            if errors_to_process:
                unsaved_errors = [e for e in errors_to_process if not e.get("saved", False)]
                if unsaved_errors:
                    params_list = [(e["type"], e["message"], e.get("request_id")) for e in unsaved_errors]

                    await db.db_execute_many(
                        """
                        INSERT INTO error_logs (error_type, error_message, request_id)
                        VALUES ($1, $2, $3)
                    """,
                        params_list,
                    )

                    for error in unsaved_errors:
                        error["saved"] = True

            self._last_save_time = time.time()
            logging.info("Metrics saved (bg): %s reqs", snapshot_data["request_count"])

            # Phase 3: Save per-user metrics
            today_str = date.today().isoformat()
            user_items = [
                (uid, data)
                for (d, uid), data in self._user_daily.items()
                if d == today_str and data["request_count"] > 0
            ]
            if user_items:
                params_list = [
                    (
                        uid,
                        snapshot_data["date"],
                        data["request_count"],
                        json.dumps(data["model_usage"]),
                    )
                    for uid, data in user_items
                ]
                await db.db_execute_many(
                    """
                    INSERT INTO user_metrics (user_id, metric_date, request_count, model_usage, updated_at)
                    VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id, metric_date) DO UPDATE SET
                        request_count = EXCLUDED.request_count,
                        model_usage = COALESCE(user_metrics.model_usage, '{}'::jsonb) || EXCLUDED.model_usage,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    params_list,
                )
                logging.debug("Per-user metrics saved for %s users", len(user_items))

                # Update streaks for active users
                try:
                    from app.repos.analytics import record_daily_activity

                    for uid, _ in user_items:
                        await record_daily_activity(uid)
                except Exception as streak_err:
                    logging.debug("Streak update skipped: %s", streak_err)

        except Exception as e:
            logging.error("Error saving metrics to database: %s", e, exc_info=True)

    async def _load_metrics_from_db(self):
        """Загружает метрики из базы данных"""
        try:
            # Load общие metrics (without JSONB полей в основном requestе)
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
                self.metrics.request_count = row["total_requests"]
                self.metrics.total_response_time = row["total_time"]
                self.metrics.error_count = row["total_errors"]
                self.metrics.search_queries = row["total_searches"]
                self.metrics.cache_hits = row["total_cache_hits"]
                self.metrics.cache_misses = row["total_cache_misses"]

            # Optimized: Aggregate JSONB fields in SQL (O(1) Python processing)
            # Aggregate api_calls
            api_calls_result = await db.db_query("""
                SELECT key, SUM(value::numeric) as total
                FROM metrics, jsonb_each_text(api_calls)
                WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY key
            """)

            self.metrics.api_calls = {row["key"]: int(row["total"]) for row in api_calls_result}

            # Aggregate model_usage
            model_usage_result = await db.db_query("""
                SELECT key, SUM(value::numeric) as total
                FROM metrics, jsonb_each_text(model_usage)
                WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY key
            """)

            self.metrics.model_usage = {row["key"]: int(row["total"]) for row in model_usage_result}
            # Load дневные metrics за afterдние 7 дней
            daily_result = await db.db_query("""
                SELECT metric_date, request_count, total_response_time, error_count,
                       search_queries, cache_hits, cache_misses, api_calls, model_usage
                FROM metrics
                WHERE metric_date >= CURRENT_DATE - INTERVAL '7 days'
                ORDER BY metric_date DESC
            """)

            for row in daily_result:
                try:
                    date_str = row["metric_date"].isoformat()
                    self.daily_metrics[date_str] = PerformanceMetrics(
                        request_count=row.get("request_count", 0) or 0,
                        total_response_time=row.get("total_response_time", 0.0) or 0.0,
                        error_count=row.get("error_count", 0) or 0,
                        search_queries=row.get("search_queries", 0) or 0,
                        cache_hits=row.get("cache_hits", 0) or 0,
                        cache_misses=row.get("cache_misses", 0) or 0,
                        api_calls=dict(row["api_calls"])
                        if row.get("api_calls") and isinstance(row["api_calls"], dict)
                        else {},
                        model_usage=dict(row["model_usage"])
                        if row.get("model_usage") and isinstance(row["model_usage"], dict)
                        else {},
                    )
                except Exception as e:
                    logging.warning("Failed to process daily metrics row: %s, row: %s", e, row)
                    continue

            # Load afterдние ошибки
            error_result = await db.db_query("""
                SELECT error_type, error_message, request_id, created_at
                FROM error_logs
                ORDER BY created_at DESC
                LIMIT 100
            """)

            self.error_log.clear()  # Clear existing before loading
            for row in error_result:
                self.error_log.append(
                    {
                        "timestamp": row["created_at"].isoformat(),
                        "type": row["error_type"],
                        "message": row["error_message"],
                        "request_id": row.get("request_id"),
                        "saved": True,  # Loaded from DB, so it is saved
                    }
                )
            logging.info("Metrics loaded from database")

        except Exception as e:
            logging.error("Error loading metrics from database: %s", e, exc_info=True)

    async def record_request(
        self, _request_type: str, response_time: float, success: bool = True, user_id: int | None = None
    ):
        """Записывает метрики запроса (Fast in-memory update)"""
        self._events_queue.put_nowait(
            {"type": "request", "response_time": response_time, "success": success, "user_id": user_id}
        )

    async def record_api_call(
        self, api_name: str, model: str | None = None, request_id: str | None = None, user_id: int | None = None
    ):
        """Записывает вызов API"""
        current_request_id = request_id or get_request_id()
        self._events_queue.put_nowait(
            {
                "type": "api_call",
                "api_name": api_name,
                "model": model,
                "request_id": current_request_id,
                "timestamp": datetime.now().isoformat(),
                "user_id": user_id,
            }
        )

    async def record_search_query(self):
        """Записывает поисковый запрос"""
        self._events_queue.put_nowait({"type": "search_query"})

    async def record_cache_hit(self):
        """Записывает попадание в кэш"""
        self._events_queue.put_nowait({"type": "cache_hit"})

    async def record_cache_miss(self):
        """Записывает промах кэша"""
        self._events_queue.put_nowait({"type": "cache_miss"})

    async def record_error(self, error_type: str, error_message: str, request_id: str | None = None):
        """Записывает ошибку"""
        current_request_id = request_id or get_request_id()
        self._events_queue.put_nowait(
            {
                "type": "error",
                "error_type": error_type,
                "error_message": error_message,
                "request_id": current_request_id,
                "timestamp": datetime.now().isoformat(),
            }
        )

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

    async def get_metrics_summary(self) -> dict[str, Any]:
        """Возвращает сводку метрик"""
        recent_errors = list(self.error_log)[-10:]

        summary = {
            "total_requests": self.metrics.request_count,
            "average_response_time": self.get_average_response_time(),
            "error_rate": self.get_error_rate(),
            "cache_hit_rate": self.get_cache_hit_rate(),
            "api_calls": dict(self.metrics.api_calls),
            "model_usage": dict(self.metrics.model_usage),
            "search_queries": self.metrics.search_queries,
            "recent_errors": recent_errors,
            "recent_api_events": list(self.api_event_log)[-20:],
            "daily_metrics": {
                date: {
                    "requests": metrics.request_count,
                    "errors": metrics.error_count,
                    "avg_response_time": metrics.total_response_time / metrics.request_count
                    if metrics.request_count > 0
                    else 0,
                }
                for date, metrics in self.daily_metrics.items()
            },
        }

        logging.debug("Metrics summary: %d requests, %.1f%% errors", summary['total_requests'], summary['error_rate'])
        return summary

    async def initialize(self):
        """Инициализирует систему метрик"""
        await self._load_metrics_from_db()
        # Start background processor
        if not self._bg_save_task:
            self._bg_save_task = asyncio.create_task(self._event_processor())
            logging.info("Metrics event processor task started")

    async def cleanup(self):
        """Очищает ресурсы и сохраняет метрики"""
        try:
            # Stop background task
            if self._bg_save_task:
                self._bg_save_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._bg_save_task
                self._bg_save_task = None

            # Check, что база данных доступна before сохранением
            if db.db_manager.is_connected:
                await self._save_metrics_to_db()
            else:
                logging.warning("Database pool unavailable during metrics cleanup, skipping save")
        except Exception as e:
            logging.error("Error during metrics cleanup: %s", e, exc_info=True)
            # Не позволяем ошибкам метрик прерывать shutdown


# Глобальный экземпляр сборщика метрик
metrics_collector = MetricsCollector()


# Re-export middleware for backward compatibility
from app.utils.metrics_middleware import MetricsMiddleware, track_metrics  # noqa: F401,E402

# ============================================================================
# ROLE AND CONVERSATION METRICS
# ============================================================================


@dataclass
class RoleMetrics:
    """Метрики использования ролей"""

    role_applications: dict[str, int] = field(default_factory=dict)  # role_key -> count
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
    llm_summarizations: int = 0
    local_summarizations: int = 0
    llm_summarization_failures: int = 0
    total_tokens_saved: int = 0
    average_summary_length: float = 0.0


class RoleConversationMetricsCollector:
    """Сборщик метрик для ролей и бесед.

    Note: No lock needed — all methods are simple counter increments
    which are atomic in single-threaded asyncio (no await between
    read and write).
    """

    def __init__(self):
        self.role_metrics = RoleMetrics()
        self.conversation_metrics = ConversationMetrics()
        self.summarization_metrics = SummarizationMetrics()

    _MAX_ROLE_ENTRIES = 500

    async def record_role_application(self, role_key: str):
        """Записывает применение роли"""
        self.role_metrics.role_applications[role_key] = self.role_metrics.role_applications.get(role_key, 0) + 1
        # Evict least-used roles when dict exceeds cap
        if len(self.role_metrics.role_applications) > self._MAX_ROLE_ENTRIES:
            least_key = min(self.role_metrics.role_applications, key=self.role_metrics.role_applications.get)  # type: ignore[arg-type]
            del self.role_metrics.role_applications[least_key]
        logging.info("Role applied: %s", role_key)

    async def record_custom_role_creation(self):
        """Записывает создание кастомной роли"""
        self.role_metrics.custom_roles_created += 1
        logging.info("Custom role created")

    async def record_role_clear(self):
        """Записывает сброс роли"""
        self.role_metrics.role_clears += 1
        logging.info("Role cleared")

    async def record_role_save(self):
        """Записывает сохранение роли"""
        self.role_metrics.role_saves += 1
        logging.info("Role saved")

    async def record_conversation_saved(self):
        """Записывает сохранение беседы"""
        self.conversation_metrics.conversations_saved += 1
        logging.info("Conversation saved")

    async def record_conversation_switched(self):
        """Записывает переключение на беседу"""
        self.conversation_metrics.conversations_switched += 1
        logging.info("Conversation switched")

    async def record_conversation_renamed(self):
        """Записывает переименование беседы"""
        self.conversation_metrics.conversations_renamed += 1
        logging.info("Conversation renamed")

    async def record_conversation_deleted(self):
        """Записывает удаление беседы"""
        self.conversation_metrics.conversations_deleted += 1
        logging.info("Conversation deleted")

    async def record_summarization(self, reason: str, tokens_saved: int, summary_length: int):
        """Записывает суммаризацию контекста"""
        self.summarization_metrics.summarizations_triggered += 1

        if "мягкий лимит" in reason:
            self.summarization_metrics.summarizations_soft_limit += 1
        elif "жёсткий лимит" in reason:
            self.summarization_metrics.summarizations_hard_limit += 1

        # Track LLM vs local tier
        if reason.startswith("llm:"):
            self.summarization_metrics.llm_summarizations += 1
        elif reason.startswith("local:"):
            self.summarization_metrics.local_summarizations += 1

        self.summarization_metrics.total_tokens_saved += tokens_saved

        # Update average summary length
        current_avg = self.summarization_metrics.average_summary_length
        count = self.summarization_metrics.summarizations_triggered
        self.summarization_metrics.average_summary_length = (current_avg * (count - 1) + summary_length) / count

        logging.info("Summarization triggered: %s, tokens saved: %d", reason, tokens_saved)

    async def get_metrics_summary(self) -> dict[str, Any]:
        """Возвращает сводку метрик"""
        return {
            "roles": {
                "applications": dict(self.role_metrics.role_applications),
                "custom_created": self.role_metrics.custom_roles_created,
                "clears": self.role_metrics.role_clears,
                "saves": self.role_metrics.role_saves,
            },
            "conversations": {
                "saved": self.conversation_metrics.conversations_saved,
                "switched": self.conversation_metrics.conversations_switched,
                "renamed": self.conversation_metrics.conversations_renamed,
                "deleted": self.conversation_metrics.conversations_deleted,
                "total": self.conversation_metrics.total_conversations,
            },
            "summarization": {
                "triggered": self.summarization_metrics.summarizations_triggered,
                "soft_limit": self.summarization_metrics.summarizations_soft_limit,
                "hard_limit": self.summarization_metrics.summarizations_hard_limit,
                "llm_tier": self.summarization_metrics.llm_summarizations,
                "local_tier": self.summarization_metrics.local_summarizations,
                "llm_failures": self.summarization_metrics.llm_summarization_failures,
                "tokens_saved": self.summarization_metrics.total_tokens_saved,
                "avg_summary_length": self.summarization_metrics.average_summary_length,
            },
        }


# Глобальный экземпляр сборщика метрик ролей и бесед
role_conv_metrics = RoleConversationMetricsCollector()


async def get_system_status_data() -> dict[str, Any]:
    """
    Агрегирует все системные metrics, вkeyая проfromводительность,
    использование API keyей и кредитов.
    """
    # 1. Метрики проfromводительности
    metrics = await metrics_collector.get_metrics_summary()

    # 2. Статус keyей Gemini
    today_pacific = time_utils.get_pacific_date()
    gemini_keys = await db.db_query(
        "SELECT key_hash, total_requests, last_used, created_at, is_default FROM api_keys"
    )

    # Get использование keyей Gemini за сегодня
    gemini_usage_map: dict[str, list[Any]] = {}
    if gemini_keys:
        all_usage = await db.db_query(
            "SELECT key_hash, model_name, request_count FROM key_usage WHERE usage_date = $1",
            (today_pacific,),
        )
        if all_usage:
            for row in all_usage:
                k = row["key_hash"]
                if k not in gemini_usage_map:
                    gemini_usage_map[k] = []
                gemini_usage_map[k].append(row)

    # 3. Статус кредитов Tavily
    current_month = time_utils.get_current_month_str()
    tavily_keys = await db.db_query(
        "SELECT key_hash, total_searches, last_used, created_at, credit_limit FROM tavily_api_keys"
    )

    # Get использование keyей Tavily за месяц
    tavily_usage_map = {}
    if tavily_keys:
        all_tavily_usage = await db.db_query(
            "SELECT key_hash, credit_usage FROM tavily_key_usage WHERE usage_month = $1",
            (current_month,),
        )
        if all_tavily_usage:
            for row in all_tavily_usage:
                tavily_usage_map[row["key_hash"]] = row["credit_usage"]

    return {
        "metrics_summary": metrics,
        "gemini": {
            "keys": gemini_keys,
            "usage_map": gemini_usage_map,
            "reset_time": time_utils.get_kyiv_reset_time(),
        },
        "tavily": {"keys": tavily_keys, "usage_map": tavily_usage_map},
    }
