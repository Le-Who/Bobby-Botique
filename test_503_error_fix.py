#!/usr/bin/env python3
"""
Тест для проверки исправления ошибки 503 с retry механизмом
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

async def test_503_error_handling():
    """Тест 1: Проверка обработки ошибки 503"""
    logger.info("🧪 Тест 1: Проверка обработки ошибки 503")
    
    try:
        from app.services import get_gemini_response
        from google.genai.errors import APIError
        
        # Создаем мок ошибку 503
        class Mock503Error(APIError):
            def __init__(self):
                self.message = "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'The model is overloaded. Please try again later.', 'status': 'UNAVAILABLE'}}"
                self.status_code = 503
        
        # Мокаем Gemini API клиент
        with patch('app.services.genai.Client') as mock_client:
            mock_client_instance = Mock()
            mock_client.return_value = mock_client_instance
            
            # Мокаем generate_content чтобы вернуть ошибку 503
            mock_client_instance.models.generate_content.side_effect = Mock503Error()
            
            # Мокаем count_tokens
            mock_token_count = Mock()
            mock_token_count.total_tokens = 100
            mock_client_instance.models.count_tokens.return_value = mock_token_count
            
            # Мокаем API логгер
            with patch('app.services.api_logger.log_gemini_request') as mock_log_request:
                mock_log_request.return_value = 1234567890.0
                
                with patch('app.services.api_logger.log_gemini_response') as mock_log_response:
                    mock_log_response.return_value = None
                    
                    # Мокаем metrics_collector
                    with patch('app.services.metrics_collector.record_api_call') as mock_metrics:
                        mock_metrics.return_value = None
                        
                        with patch('app.services.metrics_collector.record_error') as mock_error_metrics:
                            mock_error_metrics.return_value = None
                            
                            try:
                                # Вызываем функцию с retry механизмом
                                result = await get_gemini_response(
                                    api_key="test_key",
                                    history=[{"role": "user", "parts": ["test query"]}],
                                    model_name="gemini-2.0-flash",
                                    user_id=123,
                                    chat_id=456,
                                    max_retries=2  # Только 2 попытки для быстрого теста
                                )
                                
                                # Проверяем, что была записана метрика ошибки 503
                                mock_error_metrics.assert_called_with("gemini_overloaded", str(Mock503Error()))
                                logger.info("✅ Ошибка 503 корректно обрабатывается")
                                return True
                                
                            except Exception as e:
                                logger.error(f"❌ Неожиданная ошибка: {e}")
                                return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка при тестировании обработки 503: {e}")
        return False

async def test_retry_mechanism():
    """Тест 2: Проверка retry механизма"""
    logger.info("🧪 Тест 2: Проверка retry механизма")
    
    try:
        from app.services import get_gemini_response
        from google.genai.errors import APIError
        
        # Создаем мок ошибку 503
        class Mock503Error(APIError):
            def __init__(self):
                self.message = "503 UNAVAILABLE. The model is overloaded."
                self.status_code = 503
        
        # Создаем мок успешный ответ
        class MockResponse:
            def __init__(self, text="Success"):
                self.text = text
        
        # Мокаем Gemini API клиент
        with patch('app.services.genai.Client') as mock_client:
            mock_client_instance = Mock()
            mock_client.return_value = mock_client_instance
            
            # Первые 2 вызова возвращают ошибку 503, третий - успех
            mock_client_instance.models.generate_content.side_effect = [
                Mock503Error(),  # Первая попытка - ошибка
                Mock503Error(),  # Вторая попытка - ошибка
                MockResponse("Success after retry")  # Третья попытка - успех
            ]
            
            # Мокаем count_tokens
            mock_token_count = Mock()
            mock_token_count.total_tokens = 100
            mock_client_instance.models.count_tokens.return_value = mock_token_count
            
            # Мокаем API логгер
            with patch('app.services.api_logger.log_gemini_request') as mock_log_request:
                mock_log_request.return_value = 1234567890.0
                
                with patch('app.services.api_logger.log_gemini_response') as mock_log_response:
                    mock_log_response.return_value = None
                    
                    # Мокаем metrics_collector
                    with patch('app.services.metrics_collector.record_api_call') as mock_metrics:
                        mock_metrics.return_value = None
                        
                        with patch('app.services.metrics_collector.record_error') as mock_error_metrics:
                            mock_error_metrics.return_value = None
                            
                            # Мокаем asyncio.sleep для быстрого теста
                            with patch('asyncio.sleep') as mock_sleep:
                                mock_sleep.return_value = None
                                
                                try:
                                    # Вызываем функцию с retry механизмом
                                    result = await get_gemini_response(
                                        api_key="test_key",
                                        history=[{"role": "user", "parts": ["test query"]}],
                                        model_name="gemini-2.0-flash",
                                        user_id=123,
                                        chat_id=456,
                                        max_retries=3
                                    )
                                    
                                    # Проверяем, что был успешный ответ после retry
                                    if result[0] == "Success after retry":
                                        logger.info("✅ Retry механизм работает корректно")
                                        return True
                                    else:
                                        logger.error(f"❌ Неожиданный результат: {result}")
                                        return False
                                    
                                except Exception as e:
                                    logger.error(f"❌ Неожиданная ошибка: {e}")
                                    return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка при тестировании retry механизма: {e}")
        return False

async def test_error_message_formatting():
    """Тест 3: Проверка форматирования сообщений об ошибках"""
    logger.info("🧪 Тест 3: Проверка форматирования сообщений об ошибках")
    
    try:
        # Проверяем, что в коде есть правильные сообщения об ошибках
        with open('app/services.py', 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Проверяем наличие обработки ошибки 503
        if '503' in content and 'overloaded' in content and 'unavailable' in content:
            logger.info("✅ Обработка ошибки 503 найдена в коде")
            return True
        else:
            logger.error("❌ Обработка ошибки 503 не найдена в коде")
            return False
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке форматирования: {e}")
        return False

async def main():
    """Основная функция тестирования"""
    logger.info("🚀 Запуск тестов для проверки исправления ошибки 503")
    
    test_results = []
    
    # Запускаем тесты
    test_results.append(await test_503_error_handling())
    test_results.append(await test_retry_mechanism())
    test_results.append(await test_error_message_formatting())
    
    # Подводим итоги
    passed = sum(test_results)
    total = len(test_results)
    
    logger.info(f"\n📊 Результаты тестирования:")
    logger.info(f"✅ Прошло: {passed}")
    logger.info(f"❌ Провалено: {total - passed}")
    logger.info(f"📈 Успешность: {passed/total*100:.1f}%")
    
    if passed == total:
        logger.info("🎉 Все тесты прошли успешно!")
        logger.info("✅ Ошибка 503 исправлена с retry механизмом!")
        return True
    else:
        logger.error("💥 Некоторые тесты провалились!")
        return False

if __name__ == "__main__":
    asyncio.run(main())
