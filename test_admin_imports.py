#!/usr/bin/env python3
"""
Тестовый файл для проверки импортов admin модуля
"""
import sys
import os

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_admin_imports():
    """Тестирует импорты admin модуля"""
    try:
        print("🔍 Тестируем импорты admin модуля...")
        
        # Тест базовых модулей
        from app.config import settings
        print("✅ app.config - OK")
        
        from app.settings_service import get_setting, set_setting, get_all_settings, reset_to_defaults
        print("✅ app.settings_service - OK")
        
        from app.handlers.admin import admin_command, show_admin_main_menu
        print("✅ app.handlers.admin - OK")
        
        print("\n🎉 Все импорты admin модуля успешны!")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_admin_imports()
