"""
Health check system for all bot dependencies.
Provides comprehensive health monitoring and status reporting.
"""

import asyncio
import logging
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import settings
from app import database
from app.cache import redis_client, get_multi_layer_cache_stats
from app.circuit_breaker import get_circuit_breaker, GEMINI_API_CONFIG, TAVILY_API_CONFIG, TELEGRAM_API_CONFIG


@dataclass
class HealthStatus:
    """Health status for a service."""
    service: str
    status: str  # "healthy", "degraded", "unhealthy"
    response_time: float
    last_check: datetime
    details: Dict[str, Any]
    error_message: Optional[str] = None


class HealthChecker:
    """Comprehensive health checker for all bot dependencies."""
    
    def __init__(self):
        self.health_history: List[HealthStatus] = []
        self.max_history_size = 100
        self.check_interval = 30  # seconds
        self._monitoring_task: Optional[asyncio.Task] = None
        self._start_monitoring()
    
    def _start_monitoring(self):
        """Starts the health monitoring task."""
        if self._monitoring_task and not self._monitoring_task.done():
            return
        
        self._monitoring_task = asyncio.create_task(self._monitor_health())
    
    async def _monitor_health(self):
        """Continuous health monitoring loop."""
        while True:
            try:
                await asyncio.sleep(self.check_interval)
                await self.run_health_checks()
                
                # Clean up old history
                cutoff_time = datetime.now() - timedelta(hours=1)
                self.health_history = [
                    status for status in self.health_history 
                    if status.last_check > cutoff_time
                ]
                
                # Keep only recent entries
                if len(self.health_history) > self.max_history_size:
                    self.health_history = self.health_history[-self.max_history_size:]
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Health monitoring error: %s", e)
                await asyncio.sleep(60)  # Wait longer on error
    
    async def check_database_health(self) -> HealthStatus:
        """Checks database health."""
        start_time = time.time()
        status = "healthy"
        details = {}
        error_message = None
        
        try:
            if not database.db_pool:
                status = "unhealthy"
                error_message = "Database pool not initialized"
            else:
                # Test connection
                async with database.db_pool.acquire() as conn:
                    await conn.execute("SELECT 1")
                
                # Get pool stats
                if hasattr(database.db_pool, '_size'):
                    details['pool_size'] = database.db_pool._size
                    details['free_connections'] = database.db_pool._free_size
                    details['utilization'] = (
                        (database.db_pool._size - database.db_pool._free_size) / 
                        database.db_pool._maxsize * 100
                    ) if database.db_pool._maxsize > 0 else 0
                    
                    if details['utilization'] > 80:
                        status = "degraded"
                        details['warning'] = "High connection pool utilization"
                
        except Exception as e:
            status = "unhealthy"
            error_message = str(e)
            details['error_type'] = type(e).__name__
        
        response_time = time.time() - start_time
        return HealthStatus(
            service="database",
            status=status,
            response_time=response_time,
            last_check=datetime.now(),
            details=details,
            error_message=error_message
        )
    
    async def check_redis_health(self) -> HealthStatus:
        """Checks Redis health."""
        start_time = time.time()
        status = "healthy"
        details = {}
        error_message = None
        
        try:
            if not redis_client:
                status = "unhealthy"
                error_message = "Redis client not configured"
            else:
                # Test connection
                redis_client.ping()
                
                # Get Redis info
                info = redis_client.info()
                details['version'] = info.get('redis_version', 'N/A')
                details['uptime_days'] = info.get('uptime_in_days', 'N/A')
                details['used_memory'] = info.get('used_memory_human', 'N/A')
                details['connected_clients'] = info.get('connected_clients', 'N/A')
                
        except Exception as e:
            status = "unhealthy"
            error_message = str(e)
            details['error_type'] = type(e).__name__
        
        response_time = time.time() - start_time
        return HealthStatus(
            service="redis",
            status=status,
            response_time=response_time,
            last_check=datetime.now(),
            details=details,
            error_message=error_message
        )
    
    async def check_circuit_breakers_health(self) -> HealthStatus:
        """Checks circuit breakers health."""
        start_time = time.time()
        status = "healthy"
        details = {}
        error_message = None
        
        try:
            # Check all circuit breakers
            circuit_breakers = [
                ("gemini_api", get_circuit_breaker("gemini_api", GEMINI_API_CONFIG)),
                ("tavily_api", get_circuit_breaker("tavily_api", TAVILY_API_CONFIG)),
                ("telegram_api", get_circuit_breaker("telegram_api", TELEGRAM_API_CONFIG))
            ]
            
            unhealthy_count = 0
            for name, cb in circuit_breakers:
                cb_stats = cb.get_stats()
                details[name] = {
                    'state': cb_stats['state'],
                    'success_rate': cb_stats['success_rate'],
                    'total_requests': cb_stats['total_requests'],
                    'total_failures': cb_stats['total_failures']
                }
                
                if cb_stats['state'] == 'open':
                    unhealthy_count += 1
                    details[name]['warning'] = 'Circuit breaker is open'
            
            if unhealthy_count > 0:
                status = "degraded" if unhealthy_count < len(circuit_breakers) else "unhealthy"
                details['unhealthy_count'] = unhealthy_count
                
        except Exception as e:
            status = "unhealthy"
            error_message = str(e)
            details['error_type'] = type(e).__name__
        
        response_time = time.time() - start_time
        return HealthStatus(
            service="circuit_breakers",
            status=status,
            response_time=response_time,
            last_check=datetime.now(),
            details=details,
            error_message=error_message
        )
    
    async def check_cache_health(self) -> HealthStatus:
        """Checks cache system health."""
        start_time = time.time()
        status = "healthy"
        details = {}
        error_message = None
        
        try:
            # Get multi-layer cache stats
            cache_stats = await get_multi_layer_cache_stats()
            details.update(cache_stats)
            
            # Check memory cache utilization
            if 'memory' in cache_stats:
                memory_util = cache_stats['memory'].get('memory_utilization', 0)
                if memory_util > 90:
                    status = "degraded"
                    details['warning'] = f"High memory cache utilization: {memory_util:.1f}%"
            
            # Check Redis cache
            if 'redis' in cache_stats and 'error' not in cache_stats['redis']:
                details['redis_status'] = "healthy"
            else:
                details['redis_status'] = "unhealthy"
                if status == "healthy":
                    status = "degraded"
                
        except Exception as e:
            status = "unhealthy"
            error_message = str(e)
            details['error_type'] = type(e).__name__
        
        response_time = time.time() - start_time
        return HealthStatus(
            service="cache",
            status=status,
            response_time=response_time,
            last_check=datetime.now(),
            details=details,
            error_message=error_message
        )
    
    async def check_external_apis_health(self) -> HealthStatus:
        """Checks external APIs health."""
        start_time = time.time()
        status = "healthy"
        details = {}
        error_message = None
        
        try:
            # Check Gemini API (simulate with circuit breaker)
            gemini_cb = get_circuit_breaker("gemini_api", GEMINI_API_CONFIG)
            gemini_stats = gemini_cb.get_stats()
            details['gemini_api'] = {
                'state': gemini_stats['state'],
                'success_rate': gemini_stats['success_rate']
            }
            
            # Check Tavily API
            tavily_cb = get_circuit_breaker("tavily_api", TAVILY_API_CONFIG)
            tavily_stats = tavily_cb.get_stats()
            details['tavily_api'] = {
                'state': tavily_stats['state'],
                'success_rate': tavily_stats['success_rate']
            }
            
            # Check Telegram API
            telegram_cb = get_circuit_breaker("telegram_api", TELEGRAM_API_CONFIG)
            telegram_stats = telegram_cb.get_stats()
            details['telegram_api'] = {
                'state': telegram_stats['state'],
                'success_rate': telegram_stats['success_rate']
            }
            
            # Determine overall status
            open_circuits = sum(1 for cb in [gemini_cb, tavily_cb, telegram_cb] 
                              if cb.get_state().value == 'open')
            
            if open_circuits > 0:
                status = "degraded" if open_circuits < 3 else "unhealthy"
                details['open_circuits'] = open_circuits
                
        except Exception as e:
            status = "unhealthy"
            error_message = str(e)
            details['error_type'] = type(e).__name__
        
        response_time = time.time() - start_time
        return HealthStatus(
            service="external_apis",
            status=status,
            response_time=response_time,
            last_check=datetime.now(),
            details=details,
            error_message=error_message
        )
    
    async def run_health_checks(self) -> List[HealthStatus]:
        """Runs all health checks."""
        checks = [
            self.check_database_health(),
            self.check_redis_health(),
            self.check_circuit_breakers_health(),
            self.check_cache_health(),
            self.check_external_apis_health()
        ]
        
        results = await asyncio.gather(*checks, return_exceptions=True)
        
        # Process results and handle exceptions
        health_statuses = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Create error status for failed checks
                error_status = HealthStatus(
                    service=f"check_{i}",
                    status="unhealthy",
                    response_time=0.0,
                    last_check=datetime.now(),
                    details={'error_type': type(result).__name__},
                    error_message=str(result)
                )
                health_statuses.append(error_status)
            else:
                health_statuses.append(result)
        
        # Store in history
        self.health_history.extend(health_statuses)
        
        return health_statuses
    
    async def get_overall_health(self) -> Dict[str, Any]:
        """Gets overall system health status."""
        if not self.health_history:
            await self.run_health_checks()
        
        # Get latest status for each service
        latest_statuses = {}
        for status in reversed(self.health_history):
            if status.service not in latest_statuses:
                latest_statuses[status.service] = status
        
        # Calculate overall status
        status_counts = {'healthy': 0, 'degraded': 0, 'unhealthy': 0}
        for status in latest_statuses.values():
            status_counts[status.status] += 1
        
        if status_counts['unhealthy'] > 0:
            overall_status = "unhealthy"
        elif status_counts['degraded'] > 0:
            overall_status = "degraded"
        else:
            overall_status = "healthy"
        
        return {
            'overall_status': overall_status,
            'status_counts': status_counts,
            'services': {name: {
                'status': status.status,
                'response_time': status.response_time,
                'last_check': status.last_check.isoformat(),
                'details': status.details,
                'error_message': status.error_message
            } for name, status in latest_statuses.items()},
            'last_updated': datetime.now().isoformat()
        }
    
    async def shutdown(self):
        """Shuts down the health checker."""
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass


# Global health checker instance
health_checker = HealthChecker()


async def get_health_status() -> Dict[str, Any]:
    """Gets current health status."""
    return await health_checker.get_overall_health()


async def run_health_checks() -> List[HealthStatus]:
    """Runs health checks manually."""
    return await health_checker.run_health_checks()


async def shutdown_health_checker():
    """Shuts down the health checker."""
    await health_checker.shutdown()
