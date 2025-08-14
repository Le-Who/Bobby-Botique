import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from typing import Dict, Any

from ..config import settings, get_safety_settings, get_safety_mode_description
from ..database import db_query
from ..settings_service import get_setting as settings_get, set_setting as settings_set,
    get_all_settings as settings_get_all, reset_to_defaults as settings_reset
from ..utils.messaging import send_long_message
from ..utils.formatting import TelegramFormatter

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
        [InlineKeyboardButton("🗄️ Кэш", callback_data="admin_cache")],
        [InlineKeyboardButton("⚙️ Очередь", callback_data="admin_queue")],
        [InlineKeyboardButton("🚦 Ограничения", callback_data="admin_ratelimit")],
        [InlineKeyboardButton("👁️ Просмотр настроек", callback_data=ADMIN_MENU_VIEW)],
        [InlineKeyboardButton("🔄 Сброс к умолчаниям", callback_data=ADMIN_MENU_RESET)]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = """
🔧 **АДМИН-ПАНЕЛЬ GEMAI BOT**

Выберите раздел для настройки:

🛡️ **Безопасность** - режимы безопасности, fallback
🐛 **Отладка** - логирование, debug режим
⚡ **Производительность** - таймауты, retry
🔧 **Функции** - включение/отключение возможностей
🗄️ **Кэш** - управление кэшем поиска
⚙️ **Очередь** - мониторинг фоновых задач
🚦 **Ограничения** - мониторинг частоты запросов
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
**System instruction fallback** - отключение `system_instruction` при проблемах
"""

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await update.callback_query.edit_message_text(formatted_text, reply_markup=reply_markup, parse_mode=parse_mode)

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
def _cast_setting_value(setting_name: str, raw_value: Any) -> Any:
    """Кастует строковое значение из БД к типу значения в settings."""
    default_value = getattr(settings, setting_name, None)
    if default_value is None:
        return raw_value
    # Если уже нужного типа
    if isinstance(raw_value, type(default_value)):
        return raw_value
    # Каст из строки по типу значения по умолчанию
    try:
        if isinstance(default_value, bool):
            if isinstance(raw_value, str):
                return raw_value.strip().lower() == 'true'
            return bool(raw_value)
        if isinstance(default_value, int):
            return int(raw_value)
        if isinstance(default_value, float):
            return float(raw_value)
        # Для остальных типов возвращаем как есть (строка)
        return raw_value
    except Exception:
        return default_value

async def get_current_setting(setting_name: str) -> Any:
    """Получает текущее значение настройки через единый слой."""
    return await settings_get(setting_name)

async def get_all_current_settings() -> Dict[str, Dict[str, Any]]:
    """Получает все текущие настройки через единый слой."""
    return await settings_get_all()

async def update_setting(setting_name: str, value: Any) -> bool:
    """Обновляет настройку через единый слой."""
    ok = await settings_set(setting_name, value)
    if ok:
        logging.info(f"Admin setting updated: {setting_name} = {value}")
    return ok

async def reset_settings_to_defaults() -> bool:
    """Сбрасывает все настройки через единый слой."""
    ok = await settings_reset()
    if ok:
        logging.info("All settings reset to defaults")
    return ok
