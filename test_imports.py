#!/usr/bin/env python3
"""
Тестовый файл для проверки корректности импортов
"""
import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """Тестирует все основные импорты"""
    try:
        print("🔍 Тестируем импорты...")
        
        # Тест базовых модулей
        from app.config import settings
        print("✅ app.config - OK")
        
        from app.types import Task, TaskStatus, TaskPriority
        print("✅ app.types - OK")
        
        from app.queue import TaskQueue
        print("✅ app.queue - OK")
        
        from app.redis_queue import RedisQueue
        print("✅ app.redis_queue - OK")
        
        from app.rate_limiter import RateLimiter
        print("✅ app.rate_limiter - OK")
        
        from app.settings_service import SettingsService
        print("✅ app.settings_service - OK")
        
        from app.db_migrations import MigrationManager
        print("✅ app.db_migrations - OK")
        
        print("\n🎉 Все импорты успешны!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_imports()
