#!/usr/bin/env python3
"""
Тест для верификации улучшений промптов
Проверяет структуру, few-shot примеры и оптимизацию для Gemini 2.5 Pro
"""

import sys
import os
import logging
from typing import List, Dict, Any

# Добавляем путь к проекту
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class PromptImprovementsTester:
    """Тестер для проверки улучшений промптов"""
    
    def __init__(self):
        self.test_results = []
        self.prompts = {}
        self.load_prompts()
    
    def load_prompts(self):
        """Загружает промпты из файлов"""
        try:
            # Импортируем промпты
            from app.prompts import (
                QNA_LOCALIZATION_PROMPT,
                URL_SELECTION_PROMPT,
                SYNTHESIS_PROMPT,
                IMAGE_ANALYSIS_PROMPT
            )
            
            self.prompts = {
                'QNA_LOCALIZATION_PROMPT': QNA_LOCALIZATION_PROMPT,
                'URL_SELECTION_PROMPT': URL_SELECTION_PROMPT,
                'SYNTHESIS_PROMPT': SYNTHESIS_PROMPT,
                'IMAGE_ANALYSIS_PROMPT': IMAGE_ANALYSIS_PROMPT
            }
            
            # Загружаем системный промпт
            from app.config import settings
            self.prompts['DEFAULT_SYSTEM_PROMPT'] = settings.DEFAULT_SYSTEM_PROMPT
            
            logger.info("✅ Промпты успешно загружены")
            
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки промптов: {e}")
            raise
    
    def test_prompt_structure(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет структуру промпта"""
        required_sections = [
            '# РОЛЬ И ЗАДАЧА',
            '# КОНТЕКСТ',
            '# ПОШАГОВЫЕ ИНСТРУКЦИИ',
            '# ПРАВИЛА ФОРМАТИРОВАНИЯ',
            '# ПРИМЕРЫ',
            '# ВЫХОД'
        ]
        
        missing_sections = []
        for section in required_sections:
            if section not in prompt_text:
                missing_sections.append(section)
        
        if missing_sections:
            self.log_test(f"Структура {prompt_name}", False, f"Отсутствуют секции: {missing_sections}")
            return False
        
        self.log_test(f"Структура {prompt_name}", True, "Все необходимые секции присутствуют")
        return True
    
    def test_few_shot_examples(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет наличие few-shot примеров"""
        if '✅' in prompt_text and '❌' in prompt_text:
            self.log_test(f"Few-shot примеры {prompt_name}", True, "Примеры правильного и неправильного использования присутствуют")
            return True
        else:
            self.log_test(f"Few-shot примеры {prompt_name}", False, "Отсутствуют few-shot примеры")
            return False
    
    def test_chain_of_thought(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет наличие chain-of-thought инструкций"""
        thought_indicators = [
            'ПОШАГОВЫЙ',
            'ШАГ',
            '1.',
            '2.',
            '3.',
            'Сначала',
            'Затем',
            'Наконец'
        ]
        
        found_indicators = [indicator for indicator in thought_indicators if indicator in prompt_text]
        
        if len(found_indicators) >= 2:
            self.log_test(f"Chain-of-thought {prompt_name}", True, f"Найдены индикаторы: {found_indicators}")
            return True
        else:
            self.log_test(f"Chain-of-thought {prompt_name}", False, "Недостаточно chain-of-thought инструкций")
            return False
    
    def test_gemini_optimization(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет оптимизацию для Gemini"""
        gemini_indicators = [
            'Gemini',
            'структурированный',
            'пошаговый',
            'четкий',
            'логичный'
        ]
        
        found_indicators = [indicator for indicator in gemini_indicators if indicator in prompt_text]
        
        if len(found_indicators) >= 2:
            self.log_test(f"Gemini оптимизация {prompt_name}", True, f"Найдены индикаторы: {found_indicators}")
            return True
        else:
            self.log_test(f"Gemini оптимизация {prompt_name}", False, "Недостаточно индикаторов оптимизации для Gemini")
            return False
    
    def test_formatting_rules(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет правила форматирования"""
        formatting_indicators = [
            'MarkdownV2',
            '`*жирный*`',
            '`_курсив_`',
            '`` `код` ``',
            'HTML теги',
            'LaTeX'
        ]
        
        found_indicators = [indicator for indicator in formatting_indicators if indicator in prompt_text]
        
        if len(found_indicators) >= 4:
            self.log_test(f"Правила форматирования {prompt_name}", True, f"Найдены индикаторы: {found_indicators}")
            return True
        else:
            self.log_test(f"Правила форматирования {prompt_name}", False, f"Недостаточно индикаторов: {found_indicators}")
            return False
    
    def test_mathematical_formatting(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет правила математического форматирования"""
        math_indicators = [
            '2 × 3 = 6',
            '√2',
            '1/2',
            '2^3 = 8',
            'НЕ $',
            'НЕ $$'
        ]
        
        found_indicators = [indicator for indicator in math_indicators if indicator in prompt_text]
        
        if len(found_indicators) >= 3:
            self.log_test(f"Математическое форматирование {prompt_name}", True, f"Найдены индикаторы: {found_indicators}")
            return True
        else:
            self.log_test(f"Математическое форматирование {prompt_name}", False, f"Недостаточно индикаторов: {found_indicators}")
            return False
    
    def test_prompt_length_optimization(self, prompt_name: str, prompt_text: str) -> bool:
        """Проверяет оптимизацию длины промпта"""
        lines = prompt_text.split('\n')
        non_empty_lines = [line for line in lines if line.strip()]
        
        if 20 <= len(non_empty_lines) <= 100:
            self.log_test(f"Длина промпта {prompt_name}", True, f"Оптимальная длина: {len(non_empty_lines)} строк")
            return True
        else:
            self.log_test(f"Длина промпта {prompt_name}", False, f"Неподходящая длина: {len(non_empty_lines)} строк")
            return False
    
    def run_all_tests(self) -> Dict[str, bool]:
        """Запускает все тесты"""
        logger.info("🚀 Запуск тестов улучшений промптов...")
        
        for prompt_name, prompt_text in self.prompts.items():
            logger.info(f"\n📝 Тестирование: {prompt_name}")
            
            # Запускаем все тесты для каждого промпта
            tests = [
                ('Структура', self.test_prompt_structure, prompt_name, prompt_text),
                ('Few-shot примеры', self.test_few_shot_examples, prompt_name, prompt_text),
                ('Chain-of-thought', self.test_chain_of_thought, prompt_name, prompt_text),
                ('Gemini оптимизация', self.test_gemini_optimization, prompt_name, prompt_text),
                ('Правила форматирования', self.test_formatting_rules, prompt_name, prompt_text),
                ('Математическое форматирование', self.test_mathematical_formatting, prompt_name, prompt_text),
                ('Длина промпта', self.test_prompt_length_optimization, prompt_name, prompt_text)
            ]
            
            for test_name, test_func, *args in tests:
                try:
                    test_func(*args)
                except Exception as e:
                    self.log_test(f"{test_name} {prompt_name}", False, f"Ошибка теста: {e}")
        
        return self.calculate_results()
    
    def calculate_results(self) -> Dict[str, Any]:
        """Подсчитывает результаты тестов"""
        total_tests = len(self.test_results)
        passed_tests = sum(1 for result in self.test_results if result['passed'])
        failed_tests = total_tests - passed_tests
        
        success_rate = (passed_tests / total_tests * 100) if total_tests > 0 else 0
        
        results = {
            'total_tests': total_tests,
            'passed_tests': passed_tests,
            'failed_tests': failed_tests,
            'success_rate': success_rate,
            'test_results': self.test_results
        }
        
        return results
    
    def log_test(self, test_name: str, passed: bool, message: str):
        """Логирует результат теста"""
        status = "✅ ПРОЙДЕН" if passed else "❌ ПРОВАЛЕН"
        logger.info(f"{status} {test_name}: {message}")
        
        self.test_results.append({
            'test_name': test_name,
            'passed': passed,
            'message': message
        })
    
    def print_summary(self, results: Dict[str, Any]):
        """Выводит сводку результатов"""
        logger.info("\n" + "="*60)
        logger.info("📊 СВОДКА РЕЗУЛЬТАТОВ ТЕСТОВ")
        logger.info("="*60)
        logger.info(f"Всего тестов: {results['total_tests']}")
        logger.info(f"Пройдено: {results['passed_tests']} ✅")
        logger.info(f"Провалено: {results['failed_tests']} ❌")
        logger.info(f"Процент успеха: {results['success_rate']:.1f}%")
        
        if results['failed_tests'] > 0:
            logger.info("\n❌ ПРОВАЛЕННЫЕ ТЕСТЫ:")
            for result in results['test_results']:
                if not result['passed']:
                    logger.info(f"  - {result['test_name']}: {result['message']}")
        
        logger.info("\n" + "="*60)

def main():
    """Основная функция"""
    try:
        tester = PromptImprovementsTester()
        results = tester.run_all_tests()
        tester.print_summary(results)
        
        # Возвращаем код выхода
        if results['failed_tests'] == 0:
            logger.info("🎉 Все тесты пройдены успешно!")
            return 0
        else:
            logger.warning(f"⚠️ {results['failed_tests']} тестов провалено")
            return 1
            
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
