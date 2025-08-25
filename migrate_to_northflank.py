#!/usr/bin/env python3
"""
Скрипт миграции с Render.com на Northflank.com
Автоматизирует процесс подготовки проекта к новому хостингу
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

def print_step(step: str, description: str):
    """Выводит информацию о шаге миграции"""
    print(f"\n{'='*60}")
    print(f"🔧 ШАГ {step}: {description}")
    print(f"{'='*60}")

def check_requirements():
    """Проверяет наличие необходимых файлов и зависимостей"""
    print_step("1", "Проверка требований")
    
    required_files = [
        "bot.py",
        "requirements.txt",
        "Dockerfile",
        "docker-compose.yml"
    ]
    
    missing_files = []
    for file in required_files:
        if not Path(file).exists():
            missing_files.append(file)
    
    if missing_files:
        print(f"❌ Отсутствуют файлы: {', '.join(missing_files)}")
        return False
    
    print("✅ Все необходимые файлы найдены")
    return True

def backup_current_config():
    """Создает резервную копию текущей конфигурации"""
    print_step("2", "Создание резервной копии")
    
    backup_dir = Path("backup_render_config")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    
    backup_dir.mkdir()
    
    # Копируем текущие конфигурационные файлы
    files_to_backup = [
        "render.yaml",
        "Dockerfile",
        "docker-compose.yml"
    ]
    
    for file in files_to_backup:
        if Path(file).exists():
            shutil.copy2(file, backup_dir / file)
            print(f"✅ Создана резервная копия: {file}")
    
    print(f"📁 Резервная копия сохранена в: {backup_dir}")
    return True

def create_northflank_configs():
    """Создает конфигурационные файлы для Northflank.com"""
    print_step("3", "Создание конфигурации Northflank.com")
    
    # Проверяем, что файлы уже созданы
    northflank_files = [
        "northflank.yaml",
        "Dockerfile.northflank",
        "docker-compose.northflank.yml"
    ]
    
    for file in northflank_files:
        if Path(file).exists():
            print(f"✅ Файл уже существует: {file}")
        else:
            print(f"❌ Файл не найден: {file}")
            print("   Создайте файлы вручную или используйте созданные ранее")
    
    return True

def update_gitignore():
    """Обновляет .gitignore для Northflank.com"""
    print_step("4", "Обновление .gitignore")
    
    gitignore_file = Path(".gitignore")
    northflank_patterns = [
        "# Northflank.com",
        "northflank.yaml",
        ".northflank/",
        "northflank-logs/"
    ]
    
    if gitignore_file.exists():
        content = gitignore_file.read_text(encoding='utf-8')
        
        # Проверяем, есть ли уже Northflank паттерны
        has_northflank = any(pattern in content for pattern in northflank_patterns)
        
        if not has_northflank:
            with gitignore_file.open('a', encoding='utf-8') as f:
                f.write("\n# Northflank.com\n")
                f.write("northflank.yaml\n")
                f.write(".northflank/\n")
                f.write("northflank-logs/\n")
            print("✅ .gitignore обновлен для Northflank.com")
        else:
            print("✅ .gitignore уже содержит Northflank паттерны")
    else:
        print("⚠️  .gitignore не найден, создайте вручную")
    
    return True

def validate_environment_vars():
    """Проверяет наличие необходимых переменных окружения"""
    print_step("5", "Проверка переменных окружения")
    
    required_vars = [
        "TELEGRAM_BOT_TOKEN",
        "GEMINI_API_KEYS",
        "TAVILY_API_KEYS",
        "DATABASE_URL",
        "REDIS_URL",
        "ADMIN_ID"
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"⚠️  Отсутствуют переменные окружения: {', '.join(missing_vars)}")
        print("   Добавьте их в Northflank Dashboard после деплоя")
    else:
        print("✅ Все необходимые переменные окружения найдены")
    
    return True

def create_deployment_script():
    """Создает скрипт для деплоя на Northflank.com"""
    print_step("6", "Создание скрипта деплоя")
    
    deploy_script = """#!/bin/bash
# Скрипт деплоя на Northflank.com

echo "🚀 Начинаем деплой на Northflank.com..."

# 1. Убедиться, что все изменения закоммичены
echo "📝 Проверяем статус Git..."
git status

echo ""
echo "⚠️  УБЕДИТЕСЬ, что все изменения закоммичены!"
echo "   Если нет, выполните:"
echo "   git add . && git commit -m 'Prepare for Northflank migration'"
echo ""

# 2. Push в main ветку
echo "📤 Push в main ветку..."
git push origin main

echo ""
echo "✅ Код отправлен в репозиторий"
echo "🌐 Northflank автоматически развернет приложение"
echo ""
echo "📋 Следующие шаги:"
echo "   1. Откройте Northflank Dashboard"
echo "   2. Создайте новый проект"
echo "   3. Подключите Git репозиторий"
echo "   4. Настройте переменные окружения"
echo "   5. Дождитесь завершения сборки"
echo ""
echo "🔗 Документация: NORTHFLANK_MIGRATION.md"
"""
    
    script_path = Path("deploy_to_northflank.sh")
    script_path.write_text(deploy_script, encoding='utf-8')
    script_path.chmod(0o755)
    
    print("✅ Создан скрипт деплоя: deploy_to_northflank.sh")
    return True

def generate_summary():
    """Генерирует итоговую сводку миграции"""
    print_step("7", "Итоговая сводка")
    
    print("🎯 МИГРАЦИЯ ГОТОВА К ВЫПОЛНЕНИЮ!")
    print("\n📁 Созданные файлы:")
    
    new_files = [
        "northflank.yaml",
        "Dockerfile.northflank", 
        "docker-compose.northflank.yml",
        "NORTHFLANK_MIGRATION.md",
        "deploy_to_northflank.sh"
    ]
    
    for file in new_files:
        if Path(file).exists():
            print(f"   ✅ {file}")
        else:
            print(f"   ❌ {file}")
    
    print("\n📋 Следующие шаги:")
    print("   1. Изучите NORTHFLANK_MIGRATION.md")
    print("   2. Запустите: ./deploy_to_northflank.sh")
    print("   3. Следуйте инструкциям в Northflank Dashboard")
    
    print("\n⚠️  ВАЖНО:")
    print("   - Создайте резервную копию данных перед миграцией")
    print("   - Протестируйте на staging окружении")
    print("   - Мониторьте логи после деплоя")
    
    return True

def main():
    """Основная функция миграции"""
    print("🚀 МИГРАЦИЯ С RENDER.COM НА NORTHFLANK.COM")
    print("=" * 60)
    
    try:
        # Выполняем все шаги миграции
        if not check_requirements():
            return False
        
        if not backup_current_config():
            return False
        
        if not create_northflank_configs():
            return False
        
        if not update_gitignore():
            return False
        
        if not validate_environment_vars():
            return False
        
        if not create_deployment_script():
            return False
        
        if not generate_summary():
            return False
        
        print("\n🎉 МИГРАЦИЯ УСПЕШНО ПОДГОТОВЛЕНА!")
        print("Теперь вы можете следовать инструкциям для деплоя на Northflank.com")
        
    except Exception as e:
        print(f"\n❌ ОШИБКА МИГРАЦИИ: {e}")
        return False
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
