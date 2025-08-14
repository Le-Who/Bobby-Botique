#!/usr/bin/env python3
"""
Тестовый файл для проверки настроек безопасности
"""
import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def test_safety_settings():
    """Тестирует настройки безопасности"""
    try:
        print("🔍 Тестируем настройки безопасности...")
        
        # Тест базовых модулей
        from app.config import get_safety_settings, get_current_safety_mode
        print("✅ app.config - OK")
        
        from app.settings_service import get_setting
        print("✅ app.settings_service - OK")
        
        # Тестируем разные режимы безопасности
        print("\n🔒 Тестируем режимы безопасности:")
        
        # Стандартный режим
        standard = get_safety_settings("standard")
        print(f"✅ Standard: {len(standard)} правил")
        
        # Расслабленный режим
        relaxed = get_safety_settings("relaxed")
        print(f"✅ Relaxed: {len(relaxed)} правил")
        
        # Отключенный режим
        disabled = get_safety_settings("disabled")
        print(f"✅ Disabled: {len(disabled)} правил")
        
        # Агрессивный режим
        aggressive = get_safety_settings("aggressive")
        print(f"✅ Aggressive: {len(aggressive)} правил")
        
        # Текущий режим
        current_mode = get_current_safety_mode()
        print(f"✅ Current mode: {current_mode}")
        
        # Тестируем настройки для текущего режима
        current_settings = get_safety_settings(current_mode)
        print(f"✅ Current settings: {len(current_settings)} правил")
        
        print("\n🎉 Все тесты безопасности успешны!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка теста безопасности: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_safety_settings())
