#!/usr/bin/env python3
"""
Базовые тесты функциональности ролей и бесед
Проверяет, что основные компоненты импортируются и работают
"""

import sys
import os

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """Тестирует импорт основных модулей"""
    print("🧪 Тестирование импортов...")
    
    try:
        from app import prompts
        print("✅ app.prompts импортирован")
        
        from app.metrics import role_conv_metrics
        print("✅ app.metrics.role_conv_metrics импортирован")
        
        from app.handlers import commands, callbacks
        print("✅ app.handlers.commands и callbacks импортированы")
        
        from app.state import get_user_state, begin_custom_role_creation
        print("✅ app.state функции импортированы")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        return False

def test_prompts():
    """Тестирует функции промптов"""
    print("🧪 Тестирование промптов...")
    
    try:
        from app import prompts
        
        # Тестируем предустановленные роли
        assert len(prompts.DEFAULT_ROLES) > 0, "Нет предустановленных ролей"
        print(f"✅ Найдено {len(prompts.DEFAULT_ROLES)} предустановленных ролей")
        
        # Тестируем композицию системной инструкции
        system_instruction = prompts.compose_system_instruction(None)
        assert len(system_instruction) > 0, "Системная инструкция пуста"
        print("✅ Композиция системной инструкции работает")
        
        # Тестируем оценку токенов
        tokens = prompts.estimate_tokens("Тестовый текст")
        assert tokens > 0, "Оценка токенов не работает"
        print(f"✅ Оценка токенов: {tokens}")
        
        # Тестируем проверку лимитов
        should_sum, reason = prompts.should_summarize_context([])
        assert not should_sum, "Пустая история не должна требовать суммаризации"
        print("✅ Проверка лимитов работает")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка в тестах промптов: {e}")
        return False

def test_metrics():
    """Тестирует метрики"""
    print("🧪 Тестирование метрик...")
    
    try:
        from app.metrics import role_conv_metrics
        
        # Тестируем получение метрик
        metrics = role_conv_metrics.get_metrics_summary()
        assert isinstance(metrics, dict), "Метрики не являются словарём"
        assert "roles" in metrics, "Нет метрик ролей"
        assert "conversations" in metrics, "Нет метрик бесед"
        assert "summarization" in metrics, "Нет метрик суммаризации"
        print("✅ Метрики работают")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка в тестах метрик: {e}")
        return False

def test_state_management():
    """Тестирует управление состоянием"""
    print("🧪 Тестирование управления состоянием...")
    
    try:
        from app.state import get_user_state, begin_custom_role_creation, clear_custom_role_state
        
        test_user_id = 999999
        
        # Тестируем получение состояния
        state = get_user_state(test_user_id)
        assert state is not None, "Состояние не создано"
        print("✅ Получение состояния работает")
        
        # Тестируем начало создания роли
        begin_custom_role_creation(test_user_id)
        state = get_user_state(test_user_id)
        assert state.awaiting_custom_role_input, "Состояние ожидания не установлено"
        print("✅ Начало создания роли работает")
        
        # Тестируем очистку состояния
        clear_custom_role_state(test_user_id)
        state = get_user_state(test_user_id)
        assert not state.awaiting_custom_role_input, "Состояние не очищено"
        print("✅ Очистка состояния работает")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка в тестах состояния: {e}")
        return False

def run_basic_tests():
    """Запускает базовые тесты"""
    print("🚀 Запуск базовых тестов...")
    
    tests = [
        test_imports,
        test_prompts,
        test_metrics,
        test_state_management
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                print(f"❌ Тест {test.__name__} не прошёл")
        except Exception as e:
            print(f"❌ Тест {test.__name__} упал с ошибкой: {e}")
    
    print(f"\n📊 Результат: {passed}/{total} тестов прошли")
    
    if passed == total:
        print("🎉 Все базовые тесты прошли успешно!")
        return True
    else:
        print("💥 Некоторые тесты не прошли")
        return False

if __name__ == "__main__":
    success = run_basic_tests()
    sys.exit(0 if success else 1)
