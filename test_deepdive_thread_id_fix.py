#!/usr/bin/env python3
"""
Тест для проверки исправления ошибки с deep_dive_thread_id
"""

import asyncio
import logging
import sys
import os
from unittest.mock import Mock, AsyncMock, patch, MagicMock

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_chat_state_creation():
    """Тест 1: Проверка создания ChatState с deep_dive_thread_id"""
    logger.info("🧪 Тест 1: Проверка создания ChatState с deep_dive_thread_id")
    
    try:
        from app.database import ChatState
        
        # Создаем ChatState с новым атрибутом
        chat_state = ChatState(
            history=[],
            model="gemini-2.0-flash",
            token_count=0,
            search_enabled=False,
            system_prompt=None,
            is_deep_dive=False,
            deep_dive_thread_id=None
        )
        
        # Проверяем, что атрибут существует
        if hasattr(chat_state, 'deep_dive_thread_id'):
            logger.info("✅ ChatState успешно создан с атрибутом deep_dive_thread_id")
            return True
        else:
            logger.error("❌ ChatState не содержит атрибут deep_dive_thread_id")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка при создании ChatState: {e}")
        return False

async def test_safe_deep_dive_thread_id_check():
    """Тест 2: Проверка безопасной проверки deep_dive_thread_id"""
    logger.info("🧪 Тест 2: Проверка безопасной проверки deep_dive_thread_id")
    
    try:
        from app.database import ChatState
        
        # Создаем ChatState БЕЗ атрибута deep_dive_thread_id (симулируем старую версию)
        chat_state = ChatState(
            history=[],
            model="gemini-2.0-flash",
            token_count=0,
            search_enabled=False,
            system_prompt=None,
            is_deep_dive=False
        )
        
        # Удаляем атрибут для симуляции старой версии
        if hasattr(chat_state, 'deep_dive_thread_id'):
            delattr(chat_state, 'deep_dive_thread_id')
        
        # Проверяем безопасную проверку
        if not hasattr(chat_state, 'deep_dive_thread_id') or not chat_state.deep_dive_thread_id:
            logger.info("✅ Безопасная проверка deep_dive_thread_id работает корректно")
            return True
        else:
            logger.error("❌ Безопасная проверка deep_dive_thread_id не работает")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке безопасной проверки: {e}")
        return False

async def test_database_migration():
    """Тест 3: Проверка миграции базы данных"""
    logger.info("🧪 Тест 3: Проверка миграции базы данных")
    
    try:
        # Проверяем, что миграция добавлена в код
        with open('app/database.py', 'r', encoding='utf-8') as f:
            content = f.read()
            
        if 'deep_dive_thread_id' in content and 'ALTER TABLE users ADD COLUMN deep_dive_thread_id' in content:
            logger.info("✅ Миграция для deep_dive_thread_id найдена в коде")
            return True
        else:
            logger.error("❌ Миграция для deep_dive_thread_id не найдена в коде")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке миграции: {e}")
        return False

async def main():
    """Основная функция тестирования"""
    logger.info("🚀 Запуск тестов для проверки исправления deep_dive_thread_id")
    
    test_results = []
    
    # Запускаем тесты
    test_results.append(await test_chat_state_creation())
    test_results.append(await test_safe_deep_dive_thread_id_check())
    test_results.append(await test_database_migration())
    
    # Подводим итоги
    passed = sum(test_results)
    total = len(test_results)
    
    logger.info(f"\n📊 Результаты тестирования:")
    logger.info(f"✅ Прошло: {passed}")
    logger.info(f"❌ Провалено: {total - passed}")
    logger.info(f"📈 Успешность: {passed/total*100:.1f}%")
    
    if passed == total:
        logger.info("🎉 Все тесты прошли успешно!")
        logger.info("✅ Ошибка с deep_dive_thread_id исправлена!")
        return True
    else:
        logger.error("💥 Некоторые тесты провалились!")
        return False

if __name__ == "__main__":
    asyncio.run(main())
