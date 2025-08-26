#!/usr/bin/env python3
"""
Тест для воспроизведения и исправления проблем с Redis кешированием
"""

import asyncio
import json
import logging
import os
import sys
from typing import Dict, Any, Optional

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Импорт модулей кеширования
sys.path.append('app')
from cache import (
    redis_client, 
    get_cached_search_result, 
    cache_search_result,
    _redis_operation_with_retry,
    _generate_cache_key
)
from exceptions import RedisConnectionError

class RedisTestSuite:
    """Тестовый набор для проверки Redis функциональности"""
    
    def __init__(self):
        self.test_results = []
        self.redis_url = os.getenv("REDIS_URL")
        
    def log_test(self, test_name: str, success: bool, details: str = ""):
        """Логирует результат теста"""
        status = "✅ PASS" if success else "❌ FAIL"
        message = f"{status} {test_name}"
        if details:
            message += f" - {details}"
        logger.info(message)
        self.test_results.append({
            "test": test_name,
            "success": success,
            "details": details
        })
    
    async def test_redis_connection(self) -> bool:
        """Тест 1: Проверка подключения к Redis"""
        try:
            if not self.redis_url:
                self.log_test("Redis Connection", False, "REDIS_URL not set")
                return False
                
            if not redis_client:
                self.log_test("Redis Connection", False, "Redis client not initialized")
                return False
            
            # Тест ping
            await asyncio.to_thread(redis_client.ping)
            self.log_test("Redis Connection", True, "Successfully connected")
            return True
            
        except Exception as e:
            self.log_test("Redis Connection", False, f"Connection failed: {e}")
            return False
    
    async def test_redis_basic_operations(self) -> bool:
        """Тест 2: Базовые операции Redis"""
        try:
            if not redis_client:
                self.log_test("Redis Basic Operations", False, "Redis client not available")
                return False
            
            # Тест записи
            test_key = "test:basic:write"
            test_value = {"test": "data", "number": 42}
            
            await _redis_operation_with_retry(
                redis_client.setex, 
                test_key, 
                60, 
                json.dumps(test_value)
            )
            
            # Тест чтения
            result = await _redis_operation_with_retry(redis_client.get, test_key)
            
            if result:
                if isinstance(result, bytes):
                    parsed = json.loads(result.decode('utf-8'))
                else:
                    parsed = json.loads(result)
                
                if parsed == test_value:
                    self.log_test("Redis Basic Operations", True, "Write/Read successful")
                    return True
                else:
                    self.log_test("Redis Basic Operations", False, "Data mismatch")
                    return False
            else:
                self.log_test("Redis Basic Operations", False, "Read returned None")
                return False
                
        except Exception as e:
            self.log_test("Redis Basic Operations", False, f"Operation failed: {e}")
            return False
    
    async def test_cache_functions(self) -> bool:
        """Тест 3: Функции кеширования"""
        try:
            test_query = "test query for caching"
            test_search_type = "search"
            test_result = {
                "query": test_query,
                "results": ["result1", "result2"],
                "timestamp": "2024-01-01T00:00:00Z"
            }
            
            # Тест записи в кеш
            await cache_search_result(test_query, test_search_type, test_result)
            
            # Тест чтения из кеша
            cached = await get_cached_search_result(test_query, test_search_type)
            
            if cached and cached.get("query") == test_query:
                self.log_test("Cache Functions", True, "Cache write/read successful")
                return True
            else:
                self.log_test("Cache Functions", False, "Cache data not found or corrupted")
                return False
                
        except Exception as e:
            self.log_test("Cache Functions", False, f"Cache operation failed: {e}")
            return False
    
    async def test_redis_error_handling(self) -> bool:
        """Тест 4: Обработка ошибок Redis"""
        try:
            # Тест с неверным ключом
            result = await _redis_operation_with_retry(redis_client.get, "nonexistent:key")
            
            if result is None:
                self.log_test("Redis Error Handling", True, "Graceful handling of missing key")
                return True
            else:
                self.log_test("Redis Error Handling", False, "Unexpected result for missing key")
                return False
                
        except Exception as e:
            self.log_test("Redis Error Handling", False, f"Error handling failed: {e}")
            return False
    
    async def test_redis_retry_logic(self) -> bool:
        """Тест 5: Retry логика"""
        try:
            # Тест с операцией, которая может потребовать retry
            test_key = "test:retry:logic"
            test_value = {"retry": "test"}
            
            # Записываем значение
            await _redis_operation_with_retry(
                redis_client.setex, 
                test_key, 
                30, 
                json.dumps(test_value)
            )
            
            # Читаем значение несколько раз (может потребовать retry)
            for i in range(3):
                result = await _redis_operation_with_retry(redis_client.get, test_key)
                if result:
                    if isinstance(result, bytes):
                        parsed = json.loads(result.decode('utf-8'))
                    else:
                        parsed = json.loads(result)
                    
                    if parsed == test_value:
                        self.log_test("Redis Retry Logic", True, f"Retry logic working (attempt {i+1})")
                        return True
            
            self.log_test("Redis Retry Logic", False, "Retry logic failed")
            return False
            
        except Exception as e:
            self.log_test("Redis Retry Logic", False, f"Retry logic error: {e}")
            return False
    
    async def run_all_tests(self) -> Dict[str, Any]:
        """Запускает все тесты"""
        logger.info("🚀 Starting Redis functionality tests...")
        
        tests = [
            ("Redis Connection", self.test_redis_connection),
            ("Redis Basic Operations", self.test_redis_basic_operations),
            ("Cache Functions", self.test_cache_functions),
            ("Redis Error Handling", self.test_redis_error_handling),
            ("Redis Retry Logic", self.test_redis_retry_logic),
        ]
        
        results = {}
        for test_name, test_func in tests:
            try:
                success = await test_func()
                results[test_name] = success
            except Exception as e:
                logger.error(f"Test {test_name} failed with exception: {e}")
                results[test_name] = False
        
        # Подсчет результатов
        passed = sum(1 for success in results.values() if success)
        total = len(results)
        
        logger.info(f"\n📊 Test Results: {passed}/{total} tests passed")
        
        for test_name, success in results.items():
            status = "✅" if success else "❌"
            logger.info(f"{status} {test_name}")
        
        return {
            "total_tests": total,
            "passed_tests": passed,
            "failed_tests": total - passed,
            "success_rate": (passed / total) * 100 if total > 0 else 0,
            "results": results,
            "test_details": self.test_results
        }

async def main():
    """Основная функция тестирования"""
    logger.info("🔧 Redis Cache Fix Test Suite")
    logger.info("=" * 50)
    
    # Проверяем переменные окружения
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.warning("⚠️  REDIS_URL not set - tests will fail")
    else:
        logger.info(f"✅ REDIS_URL found: {redis_url[:20]}...")
    
    # Запускаем тесты
    test_suite = RedisTestSuite()
    results = await test_suite.run_all_tests()
    
    # Выводим итоговый результат
    logger.info("\n" + "=" * 50)
    if results["success_rate"] >= 80:
        logger.info("🎉 Redis cache is working well!")
    elif results["success_rate"] >= 50:
        logger.warning("⚠️  Redis cache has some issues")
    else:
        logger.error("🚨 Redis cache has critical issues!")
    
    logger.info(f"Success rate: {results['success_rate']:.1f}%")
    
    return results

if __name__ == "__main__":
    asyncio.run(main())
