#!/usr/bin/env python3
"""
Интеграционные тесты для ролей и бесед
Тестирует основные команды и сценарии работы с ролями и сохранением бесед
"""

import asyncio
import json
import logging
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import database as db
from app import prompts
from app.metrics import role_conv_metrics
from app.handlers import commands, callbacks
from app.state import get_user_state, begin_custom_role_creation, clear_custom_role_state

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MockUpdate:
    """Мок для Update объекта Telegram"""
    def __init__(self, user_id: int, message_text: str = "", callback_data: str = ""):
        self.effective_user = MagicMock()
        self.effective_user.id = user_id
        self.message = MagicMock()
        self.message.text = message_text
        self.message.reply_text = AsyncMock()
        self.message.edit_text = AsyncMock()
        self.callback_query = MagicMock()
        self.callback_query.data = callback_data
        self.callback_query.from_user = self.effective_user
        self.callback_query.answer = AsyncMock()
        self.callback_query.edit_message_text = AsyncMock()

class MockContext:
    """Мок для Context объекта Telegram"""
    def __init__(self, args: list = None):
        self.args = args or []

async def test_database_functions():
    """Тестирует функции работы с базой данных"""
    logger.info("🧪 Тестирование функций базы данных...")
    
    try:
        # Инициализируем базу данных
        await db.init_db()
        logger.info("✅ База данных инициализирована")
        
        # Тестируем создание пользователя
        test_user_id = 999999
        await db.add_user(test_user_id)
        logger.info("✅ Пользователь создан")
        
        # Тестируем создание чата
        chat_state = db.ChatState(
            history=[],
            model="gemini-2.5-pro",
            token_count=0,
            search_enabled=False,
            system_prompt=None
        )
        await db.update_user_chat(test_user_id, chat_state)
        logger.info("✅ Чат создан")
        
        # Тестируем сохранение беседы
        conv_id = await db.save_conversation(test_user_id, "Тестовая беседа")
        assert conv_id is not None, "Не удалось сохранить беседу"
        logger.info(f"✅ Беседа сохранена с ID: {conv_id}")
        
        # Тестируем получение списка бесед
        conversations = await db.get_user_conversations(test_user_id)
        assert len(conversations) > 0, "Список бесед пуст"
        logger.info(f"✅ Получен список бесед: {len(conversations)} бесед")
        
        # Тестируем переименование беседы
        success = await db.rename_conversation(test_user_id, conv_id, "Переименованная беседа")
        assert success, "Не удалось переименовать беседу"
        logger.info("✅ Беседа переименована")
        
        # Тестируем переключение на беседу
        success = await db.switch_to_conversation(test_user_id, conv_id)
        assert success, "Не удалось переключиться на беседу"
        logger.info("✅ Переключение на беседу выполнено")
        
        # Тестируем удаление беседы
        success = await db.delete_conversation(test_user_id, conv_id)
        assert success, "Не удалось удалить беседу"
        logger.info("✅ Беседа удалена")
        
        # Очистка
        await db.db_query("DELETE FROM chats WHERE user_id = $1", (test_user_id,))
        await db.db_query("DELETE FROM users WHERE user_id = $1", (test_user_id,))
        logger.info("✅ Тестовые данные очищены")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в тестах базы данных: {e}")
        raise

async def test_role_functions():
    """Тестирует функции работы с ролями"""
    logger.info("🧪 Тестирование функций ролей...")
    
    try:
        # Тестируем предустановленные роли
        assert "teacher" in prompts.DEFAULT_ROLES, "Роль teacher не найдена"
        assert "it_engineer" in prompts.DEFAULT_ROLES, "Роль it_engineer не найдена"
        logger.info("✅ Предустановленные роли найдены")
        
        # Тестируем композицию системной инструкции
        role_prompt = prompts.DEFAULT_ROLES["teacher"]["prompt"]
        system_instruction = prompts.compose_system_instruction(role_prompt)
        assert role_prompt in system_instruction, "Роль не включена в системную инструкцию"
        logger.info("✅ Композиция системной инструкции работает")
        
        # Тестируем оценку токенов
        test_text = "Это тестовый текст для проверки оценки токенов"
        tokens = prompts.estimate_tokens(test_text)
        assert tokens > 0, "Оценка токенов не работает"
        logger.info(f"✅ Оценка токенов работает: {tokens} токенов")
        
        # Тестируем проверку лимитов
        should_sum, reason = prompts.should_summarize_context([])
        assert not should_sum, "Пустая история не должна требовать суммаризации"
        logger.info("✅ Проверка лимитов работает")
        
        # Тестируем создание суммаризации
        test_messages = [
            {"role": "user", "parts": ["Привет!"]},
            {"role": "model", "parts": ["Привет! Как дела?"]},
            {"role": "user", "parts": ["Хорошо, спасибо!"]}
        ]
        summary = prompts.create_conversation_summary(test_messages)
        assert len(summary) > 0, "Суммаризация не создана"
        logger.info("✅ Создание суммаризации работает")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в тестах ролей: {e}")
        raise

async def test_context_management():
    """Тестирует управление контекстом с лимитами"""
    logger.info("🧪 Тестирование управления контекстом...")
    
    try:
        # Создаём тестовую историю
        history = []
        for i in range(100):  # Создаём много сообщений для тестирования лимитов
            history.append({
                "role": "user" if i % 2 == 0 else "model",
                "parts": [f"Тестовое сообщение номер {i} с достаточно длинным текстом для проверки лимитов токенов"]
            })
        
        # Тестируем подготовку контекста
        prepared_history, summary = prompts.prepare_context_with_limits(history, "Новое сообщение")
        
        # Должна быть суммаризация из-за большого количества сообщений
        assert len(prepared_history) < len(history), "История не была сокращена"
        assert len(summary) > 0, "Суммаризация не создана"
        logger.info(f"✅ Контекст подготовлен: {len(prepared_history)} сообщений, суммаризация: {len(summary)} символов")
        
        # Тестируем построение финального контекста
        final_context = prompts.build_context_with_summary(prepared_history, summary, "Новое сообщение")
        assert len(final_context) > 0, "Финальный контекст пуст"
        logger.info(f"✅ Финальный контекст построен: {len(final_context)} элементов")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в тестах контекста: {e}")
        raise

async def test_metrics():
    """Тестирует метрики ролей и бесед"""
    logger.info("🧪 Тестирование метрик...")
    
    try:
        # Тестируем запись метрик ролей
        await role_conv_metrics.record_role_application("teacher")
        await role_conv_metrics.record_custom_role_creation()
        await role_conv_metrics.record_role_clear()
        
        # Тестируем запись метрик бесед
        await role_conv_metrics.record_conversation_saved()
        await role_conv_metrics.record_conversation_switched()
        await role_conv_metrics.record_conversation_renamed()
        await role_conv_metrics.record_conversation_deleted()
        
        # Тестируем запись метрик суммаризации
        await role_conv_metrics.record_summarization("мягкий лимит токенов", 1000, 500)
        
        # Тестируем получение сводки метрик
        metrics = await role_conv_metrics.get_metrics_summary()
        assert "roles" in metrics, "Метрики ролей отсутствуют"
        assert "conversations" in metrics, "Метрики бесед отсутствуют"
        assert "summarization" in metrics, "Метрики суммаризации отсутствуют"
        
        logger.info("✅ Метрики работают корректно")
        logger.info(f"📊 Метрики: {json.dumps(metrics, indent=2, ensure_ascii=False)}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в тестах метрик: {e}")
        raise

async def test_commands():
    """Тестирует команды ролей и бесед"""
    logger.info("🧪 Тестирование команд...")
    
    try:
        test_user_id = 999998
        
        # Создаём тестового пользователя
        await db.add_user(test_user_id)
        
        # Тестируем команду /roles
        update = MockUpdate(test_user_id)
        context = MockContext()
        
        with patch('app.database.is_authorized', return_value=True):
            await commands.roles_command(update, context)
        
        # Проверяем, что команда выполнилась без ошибок
        update.message.reply_text.assert_called()
        logger.info("✅ Команда /roles работает")
        
        # Тестируем callback применения роли
        update = MockUpdate(test_user_id, callback_data="role_apply:teacher")
        
        with patch('app.database.is_authorized', return_value=True):
            with patch('app.database.get_user_chat', return_value=db.ChatState([], "gemini-2.5-pro", 0, False, None)):
                with patch('app.database.update_user_chat', return_value=None):
                    await callbacks.role_apply_callback(update, context)
        
        update.callback_query.edit_message_text.assert_called()
        logger.info("✅ Callback применения роли работает")
        
        # Тестируем команду /save
        update = MockUpdate(test_user_id)
        context = MockContext(["Тестовая беседа"])
        
        with patch('app.database.is_authorized', return_value=True):
            with patch('app.database.get_user_chat', return_value=db.ChatState([], "gemini-2.5-pro", 0, False, None)):
                with patch('app.database.save_conversation', return_value=123):
                    await commands.save_conversation_command(update, context)
        
        update.message.reply_text.assert_called()
        logger.info("✅ Команда /save работает")
        
        # Очистка
        await db.db_query("DELETE FROM users WHERE user_id = $1", (test_user_id,))
        logger.info("✅ Тестовые данные очищены")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в тестах команд: {e}")
        raise

async def test_custom_role_creation():
    """Тестирует создание кастомных ролей"""
    logger.info("🧪 Тестирование создания кастомных ролей...")
    
    try:
        test_user_id = 999997
        
        # Создаём тестового пользователя
        await db.add_user(test_user_id)
        
        # Тестируем начало создания роли
        begin_custom_role_creation(test_user_id)
        state = get_user_state(test_user_id)
        assert state.awaiting_custom_role_input, "Состояние ожидания ввода не установлено"
        logger.info("✅ Начало создания роли работает")
        
        # Тестируем очистку состояния
        clear_custom_role_state(test_user_id)
        state = get_user_state(test_user_id)
        assert not state.awaiting_custom_role_input, "Состояние не очищено"
        logger.info("✅ Очистка состояния работает")
        
        # Очистка
        await db.db_query("DELETE FROM users WHERE user_id = $1", (test_user_id,))
        logger.info("✅ Тестовые данные очищены")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в тестах кастомных ролей: {e}")
        raise

async def run_all_tests():
    """Запускает все тесты"""
    logger.info("🚀 Запуск интеграционных тестов...")
    
    try:
        await test_database_functions()
        await test_role_functions()
        await test_context_management()
        await test_metrics()
        await test_commands()
        await test_custom_role_creation()
        
        logger.info("🎉 Все тесты прошли успешно!")
        return True
        
    except Exception as e:
        logger.error(f"💥 Тесты завершились с ошибкой: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
