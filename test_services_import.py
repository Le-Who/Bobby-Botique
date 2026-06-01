#!/usr/bin/env python3
"""
Тестовый файл для проверки импортов services модуля
"""
import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def test_services_imports():
    """Тестирует импорты services модуля"""
    try:
        print("🔍 Тестируем импорты services модуля...")

        # Тест базовых модулей
        from app.config import settings, get_safety_settings
        print("✅ app.config - OK")

        from app.settings_service import get_int, get_bool, get_setting
        print("✅ app.settings_service - OK")

        # Тестируем импорты из services
        from app.services import get_gemini_response, tavily_search_agent
        print("✅ app.services - OK")

        # Тестируем настройки безопасности
        safety_settings = get_safety_settings("standard")
        print(f"✅ Safety settings: {len(safety_settings)} правил")

        print("\n🎉 Все импорты services модуля успешны!")
        return True

    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_services_imports())
