#!/usr/bin/env python3
"""
Тестовый скрипт для проверки инициализации базы данных
"""

import asyncio
import logging
import os
import sys

# Добавляем путь к модулям
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

async def test_database_initialization():
    """Тестирует инициализацию базы данных"""
    try:
        print("=== TESTING DATABASE INITIALIZATION ===")
        
        # Импортируем модули
        from app.config import settings
        from app import database
        
        print(f"DATABASE_URL: {'SET' if settings.DATABASE_URL else 'NOT SET'}")
        print(f"ADMIN_ID: {settings.ADMIN_ID}")
        print(f"GEMINI_API_KEYS: {len(settings.GEMINI_API_KEYS)} keys")
        print(f"TAVILY_API_KEYS: {len(settings.TAVILY_API_KEYS)} keys")
        
        # Проверяем статус БД до инициализации
        print(f"\nDatabase status before init: {database.get_database_status()}")
        print(f"Database available: {database.is_database_available()}")
        
        # Инициализируем БД
        print("\nInitializing database...")
        await database.init_db()
        
        # Проверяем статус БД после инициализации
        print(f"Database status after init: {database.get_database_status()}")
        print(f"Database available: {database.is_database_available()}")
        
        # Тестируем простой запрос
        print("\nTesting simple query...")
        result = await database.db_query("SELECT 1 as test")
        print(f"Query result: {result}")
        
        # Тестируем получение чата пользователя
        print("\nTesting user chat retrieval...")
        chat_state = await database.get_user_chat(settings.ADMIN_ID)
        print(f"Chat state: {chat_state}")
        
        print("\n=== DATABASE INITIALIZATION TEST PASSED ===")
        
    except Exception as e:
        print(f"\n=== DATABASE INITIALIZATION TEST FAILED ===")
        print(f"Error: {e}")
        logging.error(f"Test failed: {e}", exc_info=True)
        return False
    
    return True

async def test_database_functions():
    """Тестирует основные функции базы данных"""
    try:
        print("\n=== TESTING DATABASE FUNCTIONS ===")
        
        from app import database
        
        # Тестируем получение ключей
        print("Testing Gemini key retrieval...")
        gemini_key = await database.get_available_gemini_key("gemini-1.5-flash")
        print(f"Gemini key: {'Available' if gemini_key else 'Not available'}")
        
        print("Testing Tavily key retrieval...")
        tavily_key = await database.get_available_tavily_key()
        print(f"Tavily key: {'Available' if tavily_key else 'Not available'}")
        
        # Тестируем авторизацию
        print("Testing authorization...")
        is_auth = await database.is_authorized(settings.ADMIN_ID)
        print(f"Admin authorized: {is_auth}")
        
        print("\n=== DATABASE FUNCTIONS TEST PASSED ===")
        
    except Exception as e:
        print(f"\n=== DATABASE FUNCTIONS TEST FAILED ===")
        print(f"Error: {e}")
        logging.error(f"Test failed: {e}", exc_info=True)
        return False
    
    return True

async def main():
    """Главная функция тестирования"""
    print("Starting database tests...")
    
    # Тест 1: Инициализация
    init_success = await test_database_initialization()
    
    if init_success:
        # Тест 2: Функции БД
        functions_success = await test_database_functions()
        
        if functions_success:
            print("\n🎉 ALL TESTS PASSED! Database is working correctly.")
        else:
            print("\n❌ Database functions test failed.")
    else:
        print("\n❌ Database initialization test failed.")
    
    # Закрываем соединение с БД
    try:
        from app import database
        if database.db_pool:
            await database.db_pool.close()
            print("Database connection closed.")
    except Exception as e:
        print(f"Error closing database: {e}")

if __name__ == "__main__":
    asyncio.run(main())
