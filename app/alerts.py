import logging
import asyncio
from datetime import datetime, date
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from collections import defaultdict

from .config import settings
from . import database as db
from .utils import time as time_utils

@dataclass
class AlertThreshold:
    """Пороги для алертов"""
    gemini_daily_limit_percent: float = 0.8  # 80% от дневного лимита
    tavily_monthly_limit_percent: float = 0.9  # 90% от месячного лимита
    error_rate_threshold: float = 10.0  # 10% ошибок
    response_time_threshold: float = 30.0  # 30 секунд

class AlertManager:
    """Менеджер алертов"""
    
    def __init__(self):
        self.sent_alerts: Set[str] = set()  # Уже отправленные алерты
        self.alert_cooldown = 3600  # 1 час между одинаковыми алертами
        self.last_alert_time: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()
    
    def _generate_alert_key(self, alert_type: str, details: str) -> str:
        """Генерирует уникальный ключ для алерта"""
        return f"{alert_type}:{details}"
    
    async def check_gemini_limits(self) -> List[str]:
        """Проверяет лимиты Gemini API и возвращает список алертов"""
        alerts = []
        today_pacific = time_utils.get_pacific_date()
        
        all_keys = await db.db_query("SELECT * FROM api_keys")
        for key_row in all_keys:
            key_hash = key_row['key_hash']
            
            for model_name, daily_limit in settings.DAILY_LIMITS.items():
                usage = await db.db_query(
                    "SELECT request_count FROM key_usage WHERE key_hash = ? AND model_name = ? AND usage_date = ?",
                    (key_hash, model_name, today_pacific)
                )
                request_count = usage[0]['request_count'] if usage else 0
                usage_percent = (request_count / daily_limit) * 100
                
                if usage_percent >= settings.LIMIT_THRESHOLD_PERCENT * 100:
                    alert_key = self._generate_alert_key("gemini_limit", f"{key_hash[:8]}_{model_name}")
                    
                    if await self._should_send_alert(alert_key):
                        alert_msg = (
                            f"🚨 **ALERT: Gemini API Limit**\n"
                            f"Model: `{model_name}`\n"
                            f"Usage: {request_count}/{daily_limit} ({usage_percent:.1f}%)\n"
                            f"Key: `{key_row['api_key'][:10]}...`\n"
                            f"Time: {datetime.now().strftime('%H:%M:%S')}"
                        )
                        alerts.append(alert_msg)
                        await self._mark_alert_sent(alert_key)
        
        return alerts
    
    async def check_tavily_limits(self) -> List[str]:
        """Проверяет лимиты Tavily API и возвращает список алертов"""
        alerts = []
        current_month = time_utils.get_current_month_str()
        
        all_keys = await db.db_query("SELECT * FROM tavily_api_keys")
        for key_row in all_keys:
            key_hash = key_row['key_hash']
            usage = await db.db_query(
                "SELECT credit_usage FROM tavily_key_usage WHERE key_hash = ? AND usage_month = ?",
                (key_hash, current_month)
            )
            credit_usage = usage[0]['credit_usage'] if usage else 0
            usage_percent = (credit_usage / settings.TAVILY_MONTHLY_CREDIT_LIMIT) * 100
            
            if usage_percent >= settings.TAVILY_LIMIT_THRESHOLD_PERCENT * 100:
                alert_key = self._generate_alert_key("tavily_limit", key_hash[:8])
                
                if await self._should_send_alert(alert_key):
                    alert_msg = (
                        f"🚨 **ALERT: Tavily API Limit**\n"
                        f"Usage: {credit_usage}/{settings.TAVILY_MONTHLY_CREDIT_LIMIT} ({usage_percent:.1f}%)\n"
                        f"Key: `{key_row['api_key'][:10]}...`\n"
                        f"Month: {current_month}\n"
                        f"Time: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    alerts.append(alert_msg)
                    await self._mark_alert_sent(alert_key)
        
        return alerts
    
    async def check_no_available_keys(self) -> List[str]:
        """Проверяет, есть ли доступные ключи для всех моделей"""
        alerts = []
        
        for model_name in settings.AVAILABLE_MODELS:
            available_key = await db.get_available_gemini_key(model_name)
            if not available_key:
                alert_key = self._generate_alert_key("no_keys", model_name)
                
                if await self._should_send_alert(alert_key):
                    alert_msg = (
                        f"🚨 **ALERT: No Available Keys**\n"
                        f"Model: `{model_name}`\n"
                        f"Status: All keys exhausted\n"
                        f"Time: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    alerts.append(alert_msg)
                    await self._mark_alert_sent(alert_key)
        
        # Проверяем Tavily
        available_tavily_key = await db.get_available_tavily_key()
        if not available_tavily_key:
            alert_key = self._generate_alert_key("no_keys", "tavily")
            
            if await self._should_send_alert(alert_key):
                alert_msg = (
                    f"🚨 **ALERT: No Available Tavily Keys**\n"
                    f"Status: All keys exhausted\n"
                    f"Time: {datetime.now().strftime('%H:%M:%S')}"
                )
                alerts.append(alert_msg)
                await self._mark_alert_sent(alert_key)
        
        return alerts
    
    async def _should_send_alert(self, alert_key: str) -> bool:
        """Проверяет, нужно ли отправить алерт (учитывая cooldown)"""
        async with self._lock:
            if alert_key in self.sent_alerts:
                last_time = self.last_alert_time.get(alert_key)
                if last_time and (datetime.now() - last_time).total_seconds() < self.alert_cooldown:
                    return False
            
            return True
    
    async def _mark_alert_sent(self, alert_key: str):
        """Отмечает алерт как отправленный"""
        async with self._lock:
            self.sent_alerts.add(alert_key)
            self.last_alert_time[alert_key] = datetime.now()
    
    async def clear_old_alerts(self):
        """Очищает старые алерты (вызывается раз в день)"""
        async with self._lock:
            current_time = datetime.now()
            keys_to_remove = []
            
            for alert_key, last_time in self.last_alert_time.items():
                if (current_time - last_time).total_seconds() > 86400:  # 24 часа
                    keys_to_remove.append(alert_key)
            
            for key in keys_to_remove:
                self.sent_alerts.discard(key)
                del self.last_alert_time[key]

# Глобальный экземпляр менеджера алертов
alert_manager = AlertManager()

async def run_alert_checks() -> List[str]:
    """Запускает все проверки алертов и возвращает список алертов для отправки"""
    all_alerts = []
    
    # Проверяем лимиты Gemini
    gemini_alerts = await alert_manager.check_gemini_limits()
    all_alerts.extend(gemini_alerts)
    
    # Проверяем лимиты Tavily
    tavily_alerts = await alert_manager.check_tavily_limits()
    all_alerts.extend(tavily_alerts)
    
    # Проверяем доступность ключей
    no_keys_alerts = await alert_manager.check_no_available_keys()
    all_alerts.extend(no_keys_alerts)
    
    return all_alerts

async def send_alerts_to_admin(context):
    """Отправляет алерты администратору"""
    try:
        alerts = await run_alert_checks()
        if alerts:
            for alert in alerts:
                from .utils.messaging import send_formatted_message
                
                # Отправляем сообщение без parse_mode, так как send_formatted_message обработает форматирование
                await context.bot.send_message(
                    chat_id=settings.ADMIN_ID,
                    text=alert
                )
                await asyncio.sleep(1)  # Небольшая пауза между сообщениями
    except Exception as e:
        logging.error(f"Error sending alerts: {e}")

async def schedule_alert_checks(context):
    """Планировщик проверки алертов (вызывается каждые 30 минут)"""
    await send_alerts_to_admin(context)
    await alert_manager.clear_old_alerts() 