#!/usr/bin/env python3
"""
Простой тест для проверки улучшений промптов
"""

def test_prompts():
    """Проверяет основные характеристики улучшенных промптов"""
    
    print("🔍 Проверка улучшений промптов...")
    
    # Проверяем файл prompts.py
    try:
        with open('app/prompts.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        print("✅ Файл app/prompts.py успешно прочитан")
        
        # Проверяем структуру
        required_sections = [
            '# РОЛЬ И ЗАДАЧА',
            '# КОНТЕКСТ', 
            '# ПОШАГОВЫЕ ИНСТРУКЦИИ',
            '# ПРАВИЛА ФОРМАТИРОВАНИЯ',
            '# ПРИМЕРЫ'
        ]
        
        missing_sections = []
        for section in required_sections:
            if section not in content:
                missing_sections.append(section)
        
        if missing_sections:
            print(f"❌ Отсутствуют секции: {missing_sections}")
        else:
            print("✅ Все необходимые секции присутствуют")
        
        # Проверяем few-shot примеры
        if '✅' in content and '❌' in content:
            print("✅ Few-shot примеры присутствуют")
        else:
            print("❌ Few-shot примеры отсутствуют")
        
        # Проверяем chain-of-thought
        thought_indicators = ['ПОШАГОВЫЙ', 'ШАГ', '1.', '2.', '3.']
        found_indicators = [indicator for indicator in thought_indicators if indicator in content]
        
        if len(found_indicators) >= 2:
            print(f"✅ Chain-of-thought инструкции найдены: {found_indicators}")
        else:
            print("❌ Chain-of-thought инструкции отсутствуют")
        
        # Проверяем правила форматирования
        formatting_indicators = ['MarkdownV2', '`*жирный*`', '`_курсив_`', 'HTML теги', 'LaTeX']
        found_formatting = [indicator for indicator in formatting_indicators if indicator in content]
        
        if len(found_formatting) >= 4:
            print(f"✅ Правила форматирования найдены: {found_formatting}")
        else:
            print(f"❌ Недостаточно правил форматирования: {found_formatting}")
        
        # Проверяем математическое форматирование
        math_indicators = ['2 × 3 = 6', '√2', '1/2', 'НЕ $', 'НЕ $$']
        found_math = [indicator for indicator in math_indicators if indicator in content]
        
        if len(found_math) >= 3:
            print(f"✅ Правила математического форматирования найдены: {found_math}")
        else:
            print(f"❌ Недостаточно правил математического форматирования: {found_math}")
        
        # Проверяем длину промптов
        lines = content.split('\n')
        non_empty_lines = [line for line in lines if line.strip()]
        
        if 100 <= len(non_empty_lines) <= 500:
            print(f"✅ Оптимальная длина промптов: {len(non_empty_lines)} строк")
        else:
            print(f"⚠️ Неподходящая длина промптов: {len(non_empty_lines)} строк")
            
    except Exception as e:
        print(f"❌ Ошибка при проверке prompts.py: {e}")
    
    # Проверяем файл config.py
    try:
        with open('app/config.py', 'r', encoding='utf-8') as f:
            config_content = f.read()
        
        print("\n✅ Файл app/config.py успешно прочитан")
        
        # Проверяем системный промпт
        if 'DEFAULT_SYSTEM_PROMPT' in config_content:
            print("✅ Системный промпт найден")
            
            # Проверяем структуру системного промпта
            if '# РОЛЬ И ЗАДАЧА' in config_content:
                print("✅ Системный промпт имеет правильную структуру")
            else:
                print("❌ Системный промпт не имеет правильной структуры")
        else:
            print("❌ Системный промпт не найден")
            
    except Exception as e:
        print(f"❌ Ошибка при проверке config.py: {e}")
    
    print("\n🎯 Проверка завершена!")

if __name__ == "__main__":
    test_prompts()
