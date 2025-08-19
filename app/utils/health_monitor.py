"""
Health monitoring utilities for network connectivity and system status.
"""
import logging
import asyncio
import httpx
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from .network import NetworkErrorHandler

class HealthMonitor:
    """Monitors system health including network connectivity and service status."""
    
    def __init__(self):
        self.last_telegram_check = None
        self.last_database_check = None
        self.telegram_status = "unknown"
        self.database_status = "unknown"
        self.consecutive_failures = 0
        self.max_consecutive_failures = 5
        
    async def check_telegram_api(self) -> Dict[str, Any]:
        """Checks Telegram API connectivity using a real API call."""
        try:
            start_time = datetime.now()
            
            # Делаем реальный запрос к Telegram API для проверки работоспособности
            # Используем метод getMe, который не требует токена и всегда доступен
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0)) as client:
                response = await client.get("https://api.telegram.org/bot/getMe")
                
            end_time = datetime.now()
            response_time = (end_time - start_time).total_seconds()
            
            # Telegram API возвращает 200 даже для невалидных запросов
            # Главное - что сервер отвечает
            if response.status_code == 200:
                self.telegram_status = "healthy"
                self.consecutive_failures = 0
                logging.debug(f"Telegram API health check passed (response time: {response_time:.2f}s)")
            else:
                self.telegram_status = "unhealthy"
                self.consecutive_failures += 1
                logging.warning(f"Telegram API health check failed (status: {response.status_code}, consecutive failures: {self.consecutive_failures})")
            
            self.last_telegram_check = datetime.now()
            
            return {
                "status": self.telegram_status,
                "response_time": response_time,
                "last_check": self.last_telegram_check.isoformat() if self.last_telegram_check else None,
                "consecutive_failures": self.consecutive_failures
            }
            
        except Exception as e:
            self.telegram_status = "error"
            self.consecutive_failures += 1
            logging.error(f"Telegram API health check error: {e}")
            
            return {
                "status": "error",
                "error": str(e),
                "last_check": datetime.now().isoformat(),
                "consecutive_failures": self.consecutive_failures
            }
    
    async def check_database_health(self, db_query_func) -> Dict[str, Any]:
        """Checks database connectivity and health."""
        try:
            start_time = datetime.now()
            await db_query_func("SELECT 1")
            end_time = datetime.now()
            
            response_time = (end_time - start_time).total_seconds()
            
            self.database_status = "healthy"
            self.last_database_check = datetime.now()
            
            logging.debug(f"Database health check passed (response time: {response_time:.2f}s)")
            
            return {
                "status": "healthy",
                "response_time": response_time,
                "last_check": self.last_database_check.isoformat()
            }
            
        except Exception as e:
            self.database_status = "error"
            logging.error(f"Database health check failed: {e}")
            
            return {
                "status": "error",
                "error": str(e),
                "last_check": datetime.now().isoformat()
            }
    
    async def check_external_services(self) -> Dict[str, Any]:
        """Checks external service dependencies."""
        services = {
            "tavily_api": "https://api.tavily.com",
            "gemini_api": "https://generativelanguage.googleapis.com"
        }
        
        results = {}
        
        for service_name, url in services.items():
            try:
                # Увеличиваем таймаут для внешних сервисов
                is_connected = await NetworkErrorHandler.check_connectivity(url, timeout=5.0)
                
                if is_connected:
                    results[service_name] = {
                        "status": "healthy",
                        "url": url,
                        "message": "Service is reachable"
                    }
                else:
                    results[service_name] = {
                        "status": "warning",
                        "url": url,
                        "message": "Service is unreachable but this may be normal"
                    }
                    
            except Exception as e:
                # Внешние сервисы не критичны для работы бота
                results[service_name] = {
                    "status": "warning",
                    "error": str(e),
                    "url": url,
                    "message": "Service check failed but this is not critical"
                }
        
        return results
    
    async def get_system_health_report(self, db_query_func) -> Dict[str, Any]:
        """Generates a comprehensive system health report."""
        telegram_health = await self.check_telegram_api()
        database_health = await self.check_database_health(db_query_func)
        external_services = await self.check_external_services()
        
        # Determine overall system status
        # Только Telegram API и база данных критичны для работы бота
        critical_services = [telegram_health["status"], database_health["status"]]
        
        if any(status == "error" for status in critical_services):
            overall_status = "critical"
        elif any(status == "unhealthy" for status in critical_services):
            overall_status = "degraded"
        else:
            overall_status = "healthy"
        
        # Внешние сервисы могут быть недоступны, но это не критично
        external_warnings = [s for s in external_services.values() if s.get("status") == "warning"]
        if external_warnings:
            logging.info(f"External services have warnings: {[s.get('message', 'Unknown') for s in external_warnings]}")
        
        # Логируем детальную информацию о статусе
        logging.debug(f"Health check results - Telegram: {telegram_health['status']}, Database: {database_health['status']}, Overall: {overall_status}")
        
        if self.consecutive_failures >= self.max_consecutive_failures:
            overall_status = "critical"
            logging.warning(f"System marked as critical due to {self.consecutive_failures} consecutive failures")
        
        return {
            "timestamp": datetime.now().isoformat(),
            "overall_status": overall_status,
            "telegram_api": telegram_health,
            "database": database_health,
            "external_services": external_services,
            "consecutive_failures": self.consecutive_failures,
            "recommendations": self._generate_recommendations(telegram_health, database_health, external_services)
        }
    
    def _generate_recommendations(self, telegram_health: Dict, database_health: Dict, external_services: Dict) -> list:
        """Generates recommendations based on health status."""
        recommendations = []
        
        if telegram_health["status"] != "healthy":
            if telegram_health["status"] == "error":
                recommendations.append("Telegram API connectivity issues detected. Check network connection and API status.")
            else:
                recommendations.append("Telegram API response time is slow but service is operational.")
        
        if database_health["status"] != "healthy":
            recommendations.append("Database connectivity issues detected. Check database server and connection pool.")
        
        if self.consecutive_failures >= self.max_consecutive_failures:
            recommendations.append("Multiple consecutive failures detected. Consider restarting the bot.")
        
        # Внешние сервисы - только информационные сообщения
        external_warnings = [s for s in external_services.values() if s.get("status") == "warning"]
        if external_warnings:
            for service_name, service_health in external_services.items():
                if service_health["status"] == "warning":
                    recommendations.append(f"{service_name}: {service_health.get('message', 'Service check failed')} - This is not critical for bot operation.")
        
        if not recommendations:
            recommendations.append("All systems operational.")
        
        return recommendations

# Global health monitor instance
health_monitor = HealthMonitor()
