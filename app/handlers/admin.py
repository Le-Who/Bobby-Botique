import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from typing import Dict, Any

from ..config import settings, get_safety_settings, get_safety_mode_description
from ..database import db_query
from ..utils.messaging import send_long_message

# Константы для админ-меню
ADMIN_MENU_MAIN = "admin_main"
ADMIN_MENU_SAFETY = "admin_safety"
ADMIN_MENU_DEBUG = "admin_debug"
ADMIN_MENU_PERFORMANCE = "admin_performance"
ADMIN_MENU_FEATURES = "admin_features"
ADMIN_MENU_VIEW = "admin_view"
ADMIN_MENU_RESET = "admin_reset"

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /admin - главное меню администратора"""
    user_id = update.effective_user.id
    
    # Проверяем права администратора
    if user_id != settings.ADMIN_ID:
        await update.message.reply_text("🚫 У вас нет прав для доступа к админ-панели.")
        return
    
    await show_admin_main_menu(update, context)

async def show_admin_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню администратора"""
    keyboard = [
        [InlineKeyboardButton("🛡️ Безопасность", callback_data=ADMIN_MENU_SAFETY)],
        [InlineKeyboardButton("🐛 Отладка", callback_data=ADMIN_MENU_DEBUG)],
        [InlineKeyboardButton("⚡ Производительность", callback_data=ADMIN_MENU_PERFORMANCE)],
        [InlineKeyboardButton("🔧 Функции", callback_data=ADMIN_MENU_FEATURES)],
        [InlineKeyboardButton("👁️ Просмотр настроек", callback_data=ADMIN_MENU_VIEW)],
        [InlineKeyboardButton("🔄 Сброс к умолчаниям", callback_data=ADMIN_MENU_RESET)]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = """
🔧 **АДМИН-ПАНЕЛЬ GEMAI BOT**

Выберите раздел для настройки:

🛡️ **Безопасность** - режимы безопасности, fallback
🐛 **Отладка** - логирование, debug режим
⚡ **Производительность** - кэш, таймауты, retry
🔧 **Функции** - включение/отключение возможностей
👁️ **Просмотр** - текущие настройки
🔄 **Сброс** - восстановление умолчаний

⚠️ **Внимание**: Изменения применяются немедленно!
"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_safety_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настроек безопасности"""
    current_mode = await get_current_setting("SAFETY_MODE")
    current_fallback = await get_current_setting("ENABLE_SAFETY_FALLBACK")
    
    keyboard = [
        [InlineKeyboardButton(f"🔒 Стандартный ({'✅' if current_mode == 'standard' else '❌'})", 
                            callback_data="admin_safety_mode_standard")],
        [InlineKeyboardButton(f"🟡 Расслабленный ({'✅' if current_mode == 'relaxed' else '❌'})", 
                            callback_data="admin_safety_mode_relaxed")],
        [InlineKeyboardButton(f"🟢 Отключенный ({'✅' if current_mode == 'disabled' else '❌'})", 
                            callback_data="admin_safety_mode_disabled")],
        [InlineKeyboardButton(f"🔴 Агрессивный ({'✅' if current_mode == 'aggressive' else '❌'})", 
                            callback_data="admin_safety_mode_aggressive")],
        [InlineKeyboardButton(f"🔄 Автоматический ({'✅' if current_mode == 'auto' else '❌'})", 
                            callback_data="admin_safety_mode_auto")],
        [InlineKeyboardButton(f"🔄 Fallback: {'✅' if current_fallback else '❌'}", 
                            callback_data="admin_safety_fallback_toggle")],
        [InlineKeyboardButton("📖 Описание режимов", callback_data="admin_safety_help")],
        [InlineKeyboardButton("🔙 Назад", callback_data=ADMIN_MENU_MAIN)]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
🛡️ **НАСТРОЙКИ БЕЗОПАСНОСТИ**

**Текущий режим**: {current_mode.upper()}
**Fallback**: {'Включен' if current_fallback else 'Отключен'}

**Доступные режимы:**
🔒 **standard** - Стандартные настройки
🟡 **relaxed** - Расслабленные настройки  
🟢 **disabled** - Отключенные (только для отладки)
🔴 **aggressive** - Агрессивные настройки
🔄 **auto** - Автоматическое переключение

**Fallback** - автоматическое переключение настроек при проблемах
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_debug_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настроек отладки"""
    current_debug = await get_current_setting("DEBUG_MODE")
    current_log_level = await get_current_setting("LOG_LEVEL")
    current_log_safety = await get_current_setting("LOG_SAFETY_DECISIONS")
    
    keyboard = [
        [InlineKeyboardButton(f"🐛 Debug режим: {'✅' if current_debug else '❌'}", 
                            callback_data="admin_debug_mode_toggle")],
        [InlineKeyboardButton(f"📊 Уровень логов: {current_log_level}", 
                            callback_data="admin_log_level_menu")],
        [InlineKeyboardButton(f"🛡️ Логи безопасности: {'✅' if current_log_safety else '❌'}", 
                            callback_data="admin_log_safety_toggle")],
        [InlineKeyboardButton("🔙 Назад", callback_data=ADMIN_MENU_MAIN)]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
🐛 **НАСТРОЙКИ ОТЛАДКИ**

**Debug режим**: {'Включен' if current_debug else 'Отключен'}
**Уровень логов**: {current_log_level}
**Логи безопасности**: {'Включены' if current_log_safety else 'Отключены'}

**Debug режим** - подробная информация о работе бота
**Уровень логов** - детализация записываемой информации
**Логи безопасности** - отслеживание решений по безопасности
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_performance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настроек производительности"""
    current_cache = await get_current_setting("ENABLE_CACHE")
    current_ttl = await get_current_setting("CACHE_TTL_HOURS")
    current_retries = await get_current_setting("MAX_RETRIES")
    current_timeout = await get_current_setting("REQUEST_TIMEOUT_SECONDS")
    
    keyboard = [
        [InlineKeyboardButton(f"💾 Кэш: {'✅' if current_cache else '❌'}", 
                            callback_data="admin_cache_toggle")],
        [InlineKeyboardButton(f"⏰ TTL кэша: {current_ttl}ч", 
                            callback_data="admin_cache_ttl_menu")],
        [InlineKeyboardButton(f"🔄 Макс попыток: {current_retries}", 
                            callback_data="admin_retries_menu")],
        [InlineKeyboardButton(f"⏱️ Таймаут: {current_timeout}с", 
                            callback_data="admin_timeout_menu")],
        [InlineKeyboardButton("🔙 Назад", callback_data=ADMIN_MENU_MAIN)]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
⚡ **НАСТРОЙКИ ПРОИЗВОДИТЕЛЬНОСТИ**

**Кэширование**: {'Включено' if current_cache else 'Отключено'}
**TTL кэша**: {current_ttl} часов
**Макс попыток**: {current_retries}
**Таймаут запросов**: {current_timeout} секунд

**Кэш** - ускорение повторных запросов
**TTL** - время жизни записей в кэше
**Попытки** - количество повторов при ошибках
**Таймаут** - максимальное время ожидания ответа
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_features_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настроек функций"""
    current_safety_fallback = await get_current_setting("ENABLE_SAFETY_FALLBACK")
    current_prompt_simplification = await get_current_setting("ENABLE_PROMPT_SIMPLIFICATION")
    current_system_fallback = await get_current_setting("ENABLE_SYSTEM_INSTRUCTION_FALLBACK")
    
    keyboard = [
        [InlineKeyboardButton(f"🛡️ Safety fallback: {'✅' if current_safety_fallback else '❌'}", 
                            callback_data="admin_safety_fallback_toggle")],
        [InlineKeyboardButton(f"✂️ Упрощение промптов: {'✅' if current_prompt_simplification else '❌'}", 
                            callback_data="admin_prompt_simplification_toggle")],
        [InlineKeyboardButton(f"📝 System instruction fallback: {'✅' if current_system_fallback else '❌'}", 
                            callback_data="admin_system_fallback_toggle")],
        [InlineKeyboardButton("🔙 Назад", callback_data=ADMIN_MENU_MAIN)]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
🔧 **НАСТРОЙКИ ФУНКЦИЙ**

**Safety fallback**: {'Включен' if current_safety_fallback else 'Отключен'}
**Упрощение промптов**: {'Включено' if current_prompt_simplification else 'Отключено'}
**System instruction fallback**: {'Включен' if current_system_fallback else 'Отключен'}

**Safety fallback** - автоматическое переключение настроек безопасности
**Упрощение промптов** - удаление проблемных символов при ошибках
**System instruction fallback** - отключение system_instruction при проблемах
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_current_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущие настройки"""
    settings_data = await get_all_current_settings()
    
    text = "👁️ **ТЕКУЩИЕ НАСТРОЙКИ**\n\n"
    
    for category, options in settings_data.items():
        text += f"**{category}:**\n"
        for key, value in options.items():
            text += f"  {key}: `{value}`\n"
        text += "\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=ADMIN_MENU_MAIN)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def reset_to_defaults(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает настройки к умолчаниям"""
    try:
        await reset_settings_to_defaults()
        
        text = """
🔄 **НАСТРОЙКИ СБРОШЕНЫ К УМОЛЧАНИЯМ**

Все настройки восстановлены к значениям по умолчанию:
- SAFETY_MODE: auto
- DEBUG_MODE: false
- LOG_LEVEL: INFO
- ENABLE_CACHE: true
- CACHE_TTL_HOURS: 72
- MAX_RETRIES: 3
- REQUEST_TIMEOUT_SECONDS: 60
- ENABLE_SAFETY_FALLBACK: true
- ENABLE_PROMPT_SIMPLIFICATION: true
- ENABLE_SYSTEM_INSTRUCTION_FALLBACK: true

⚠️ **Перезапустите бота для применения изменений!**
"""
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=ADMIN_MENU_MAIN)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        logging.error(f"Failed to reset settings: {e}")
        await update.callback_query.answer("❌ Ошибка при сбросе настроек", show_alert=True)

# Вспомогательные функции для работы с настройками
async def get_current_setting(setting_name: str) -> Any:
    """Получает текущее значение настройки из базы данных или конфигурации"""
    try:
        # Сначала пробуем получить из базы данных
        result = await db_query(
            "SELECT value FROM bot_settings WHERE setting_name = $1",
            setting_name
        )
        
        if result and result[0]:
            return result[0]['value']
        
        # Если в базе нет, возвращаем из конфигурации
        return getattr(settings, setting_name, None)
        
    except Exception as e:
        logging.warning(f"Could not get setting {setting_name} from database: {e}")
        # Возвращаем значение из конфигурации как fallback
        return getattr(settings, setting_name, None)

async def get_all_current_settings() -> Dict[str, Dict[str, Any]]:
    """Получает все текущие настройки, сгруппированные по категориям"""
    try:
        result = await db_query("SELECT setting_name, value FROM bot_settings")
        
        settings_dict = {}
        for row in result:
            setting_name = row['setting_name']
            value = row['value']
            
            # Группируем по категориям
            if setting_name.startswith('SAFETY_'):
                category = 'Безопасность'
            elif setting_name.startswith('DEBUG_') or setting_name.startswith('LOG_'):
                category = 'Отладка'
            elif setting_name.startswith('ENABLE_') or setting_name.startswith('CACHE_'):
                category = 'Производительность'
            elif setting_name.startswith('MAX_') or setting_name.startswith('REQUEST_'):
                category = 'Производительность'
            else:
                category = 'Прочее'
            
            if category not in settings_dict:
                settings_dict[category] = {}
            
            settings_dict[category][setting_name] = value
        
        return settings_dict
        
    except Exception as e:
        logging.error(f"Error getting all settings: {e}")
        return {}

async def update_setting(setting_name: str, value: Any) -> bool:
    """Обновляет настройку в базе данных"""
    try:
        await db_query(
            """
            INSERT INTO bot_settings (setting_name, value, updated_at) 
            VALUES ($1, $2, NOW())
            ON CONFLICT (setting_name) 
            DO UPDATE SET value = $2, updated_at = NOW()
            """,
            setting_name, str(value)
        )
        
        # Логируем изменение
        logging.info(f"Admin setting updated: {setting_name} = {value}")
        return True
        
    except Exception as e:
        logging.error(f"Error updating setting {setting_name}: {e}")
        return False

async def reset_settings_to_defaults() -> bool:
    """Сбрасывает все настройки к умолчаниям"""
    try:
        # Удаляем все пользовательские настройки
        await db_query("DELETE FROM bot_settings")
        
        logging.info("All settings reset to defaults")
        return True
        
    except Exception as e:
        logging.error(f"Error resetting settings: {e}")
        return False
