import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from typing import Dict, Any

from .admin import (
    show_admin_main_menu, show_safety_menu, show_debug_menu, 
    show_performance_menu, show_features_menu, show_current_settings,
    reset_to_defaults, update_setting, get_current_setting
)
from ..config import settings

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех callback'ов админ-меню"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Проверяем права администратора
    if user_id != settings.ADMIN_ID:
        await query.answer("🚫 У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await query.answer()
    
    callback_data = query.data
    
    try:
        if callback_data == "admin_main":
            await show_admin_main_menu(update, context)
            
        elif callback_data == "admin_safety":
            await show_safety_menu(update, context)
            
        elif callback_data == "admin_debug":
            await show_debug_menu(update, context)
            
        elif callback_data == "admin_performance":
            await show_performance_menu(update, context)
            
        elif callback_data == "admin_features":
            await show_features_menu(update, context)
            
        elif callback_data == "admin_view":
            await show_current_settings(update, context)
            
        elif callback_data == "admin_reset":
            await reset_to_defaults(update, context)
            
        # Обработка настроек безопасности
        elif callback_data.startswith("admin_safety_mode_"):
            mode = callback_data.replace("admin_safety_mode_", "")
            await handle_safety_mode_change(update, context, mode)
            
        elif callback_data == "admin_safety_fallback_toggle":
            await handle_safety_fallback_toggle(update, context)
            
        elif callback_data == "admin_safety_help":
            await show_safety_help(update, context)
            
        # Обработка настроек отладки
        elif callback_data == "admin_debug_mode_toggle":
            await handle_debug_mode_toggle(update, context)
            
        elif callback_data == "admin_log_level_menu":
            await show_log_level_menu(update, context)
            
        elif callback_data == "admin_log_safety_toggle":
            await handle_log_safety_toggle(update, context)
            
        # Обработка настроек производительности
        elif callback_data == "admin_cache_toggle":
            await handle_cache_toggle(update, context)
            
        elif callback_data == "admin_cache_ttl_menu":
            await show_cache_ttl_menu(update, context)
            
        elif callback_data == "admin_retries_menu":
            await show_retries_menu(update, context)
            
        elif callback_data == "admin_timeout_menu":
            await show_timeout_menu(update, context)
            
        # Обработка настроек функций
        elif callback_data == "admin_prompt_simplification_toggle":
            await handle_prompt_simplification_toggle(update, context)
            
        elif callback_data == "admin_system_fallback_toggle":
            await handle_system_fallback_toggle(update, context)
            
        # Обработка уровней логов
        elif callback_data.startswith("admin_log_level_"):
            level = callback_data.replace("admin_log_level_", "")
            await handle_log_level_change(update, context, level)
            
        # Обработка TTL кэша
        elif callback_data.startswith("admin_cache_ttl_"):
            ttl = callback_data.replace("admin_cache_ttl_", "")
            await handle_cache_ttl_change(update, context, ttl)
            
        # Обработка количества попыток
        elif callback_data.startswith("admin_retries_"):
            retries = callback_data.replace("admin_retries_", "")
            await handle_retries_change(update, context, retries)
            
        # Обработка таймаута
        elif callback_data.startswith("admin_timeout_"):
            timeout = callback_data.replace("admin_timeout_", "")
            await handle_timeout_change(update, context, timeout)
            
        else:
            logging.warning(f"Unknown admin callback: {callback_data}")
            await query.answer("❌ Неизвестная команда", show_alert=True)
            
    except Exception as e:
        logging.error(f"Error handling admin callback {callback_data}: {e}")
        await query.answer("❌ Произошла ошибка при обработке команды", show_alert=True)

# Обработчики настроек безопасности
async def handle_safety_mode_change(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Изменяет режим безопасности"""
    success = await update_setting("SAFETY_MODE", mode)
    
    if success:
        await update.callback_query.answer(f"✅ Режим безопасности изменен на: {mode}")
        await show_safety_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении режима", show_alert=True)

async def handle_safety_fallback_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает safety fallback"""
    current = await get_current_setting("ENABLE_SAFETY_FALLBACK")
    new_value = not current
    
    success = await update_setting("ENABLE_SAFETY_FALLBACK", new_value)
    
    if success:
        status = "включен" if new_value else "отключен"
        await update.callback_query.answer(f"✅ Safety fallback {status}")
        await show_safety_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении настройки", show_alert=True)

async def show_safety_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает справку по режимам безопасности"""
    from ..config import get_safety_mode_description
    
    help_text = get_safety_mode_description()
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_safety")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode='Markdown')

# Обработчики настроек отладки
async def handle_debug_mode_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает debug режим"""
    current = await get_current_setting("DEBUG_MODE")
    new_value = not current
    
    success = await update_setting("DEBUG_MODE", new_value)
    
    if success:
        status = "включен" if new_value else "отключен"
        await update.callback_query.answer(f"✅ Debug режим {status}")
        await show_debug_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении настройки", show_alert=True)

async def show_log_level_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню выбора уровня логов"""
    current_level = await get_current_setting("LOG_LEVEL")
    
    keyboard = [
        [InlineKeyboardButton(f"DEBUG ({'✅' if current_level == 'DEBUG' else '❌'})", 
                            callback_data="admin_log_level_DEBUG")],
        [InlineKeyboardButton(f"INFO ({'✅' if current_level == 'INFO' else '❌'})", 
                            callback_data="admin_log_level_INFO")],
        [InlineKeyboardButton(f"WARNING ({'✅' if current_level == 'WARNING' else '❌'})", 
                            callback_data="admin_log_level_WARNING")],
        [InlineKeyboardButton(f"ERROR ({'✅' if current_level == 'ERROR' else '❌'})", 
                            callback_data="admin_log_level_ERROR")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_debug")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
📊 **ВЫБОР УРОВНЯ ЛОГИРОВАНИЯ**

**Текущий уровень**: {current_level}

**Доступные уровни:**
- **DEBUG** - максимальная детализация
- **INFO** - основная информация
- **WARNING** - только предупреждения и ошибки
- **ERROR** - только ошибки

⚠️ **Внимание**: Изменения применяются немедленно!
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_log_level_change(update: Update, context: ContextTypes.DEFAULT_TYPE, level: str):
    """Изменяет уровень логирования"""
    success = await update_setting("LOG_LEVEL", level)
    
    if success:
        await update.callback_query.answer(f"✅ Уровень логов изменен на: {level}")
        await show_debug_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении уровня логов", show_alert=True)

async def handle_log_safety_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает логирование решений по безопасности"""
    current = await get_current_setting("LOG_SAFETY_DECISIONS")
    new_value = not current
    
    success = await update_setting("LOG_SAFETY_DECISIONS", new_value)
    
    if success:
        status = "включены" if new_value else "отключены"
        await update.callback_query.answer(f"✅ Логи безопасности {status}")
        await show_debug_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении настройки", show_alert=True)

# Обработчики настроек производительности
async def handle_cache_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает кэширование"""
    current = await get_current_setting("ENABLE_CACHE")
    new_value = not current
    
    success = await update_setting("ENABLE_CACHE", new_value)
    
    if success:
        status = "включено" if new_value else "отключено"
        await update.callback_query.answer(f"✅ Кэширование {status}")
        await show_performance_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении настройки", show_alert=True)

async def show_cache_ttl_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню выбора TTL кэша"""
    current_ttl = await get_current_setting("CACHE_TTL_HOURS")
    
    keyboard = [
        [InlineKeyboardButton(f"24 часа ({'✅' if current_ttl == 24 else '❌'})", 
                            callback_data="admin_cache_ttl_24")],
        [InlineKeyboardButton(f"48 часов ({'✅' if current_ttl == 48 else '❌'})", 
                            callback_data="admin_cache_ttl_48")],
        [InlineKeyboardButton(f"72 часа ({'✅' if current_ttl == 72 else '❌'})", 
                            callback_data="admin_cache_ttl_72")],
        [InlineKeyboardButton(f"168 часов (7 дней) ({'✅' if current_ttl == 168 else '❌'})", 
                            callback_data="admin_cache_ttl_168")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_performance")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
⏰ **ВЫБОР TTL КЭША**

**Текущий TTL**: {current_ttl} часов

**Доступные значения:**
- **24 часа** - для активного использования
- **48 часов** - для умеренного использования
- **72 часа** - для редкого использования (рекомендуется)
- **168 часов** - для очень редкого использования

⚠️ **Внимание**: Меньший TTL = больше свежести, больший TTL = больше экономии
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_cache_ttl_change(update: Update, context: ContextTypes.DEFAULT_TYPE, ttl: str):
    """Изменяет TTL кэша"""
    ttl_value = int(ttl)
    success = await update_setting("CACHE_TTL_HOURS", ttl_value)
    
    if success:
        await update.callback_query.answer(f"✅ TTL кэша изменен на: {ttl_value} часов")
        await show_performance_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении TTL кэша", show_alert=True)

async def show_retries_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню выбора количества попыток"""
    current_retries = await get_current_setting("MAX_RETRIES")
    
    keyboard = [
        [InlineKeyboardButton(f"1 попытка ({'✅' if current_retries == 1 else '❌'})", 
                            callback_data="admin_retries_1")],
        [InlineKeyboardButton(f"2 попытки ({'✅' if current_retries == 2 else '❌'})", 
                            callback_data="admin_retries_2")],
        [InlineKeyboardButton(f"3 попытки ({'✅' if current_retries == 3 else '❌'})", 
                            callback_data="admin_retries_3")],
        [InlineKeyboardButton(f"5 попыток ({'✅' if current_retries == 5 else '❌'})", 
                            callback_data="admin_retries_5")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_performance")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
🔄 **ВЫБОР КОЛИЧЕСТВА ПОПЫТОК**

**Текущее количество**: {current_retries}

**Доступные значения:**
- **1 попытка** - быстро, но может не сработать
- **2 попытки** - баланс скорости и надежности
- **3 попытки** - рекомендуемое значение
- **5 попыток** - максимальная надежность

⚠️ **Внимание**: Больше попыток = больше надежность, но медленнее
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_retries_change(update: Update, context: ContextTypes.DEFAULT_TYPE, retries: str):
    """Изменяет количество попыток"""
    retries_value = int(retries)
    success = await update_setting("MAX_RETRIES", retries_value)
    
    if success:
        await update.callback_query.answer(f"✅ Количество попыток изменено на: {retries_value}")
        await show_performance_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении количества попыток", show_alert=True)

async def show_timeout_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню выбора таймаута"""
    current_timeout = await get_current_setting("REQUEST_TIMEOUT_SECONDS")
    
    keyboard = [
        [InlineKeyboardButton(f"30 секунд ({'✅' if current_timeout == 30 else '❌'})", 
                            callback_data="admin_timeout_30")],
        [InlineKeyboardButton(f"60 секунд ({'✅' if current_timeout == 60 else '❌'})", 
                            callback_data="admin_timeout_60")],
        [InlineKeyboardButton(f"120 секунд ({'✅' if current_timeout == 120 else '❌'})", 
                            callback_data="admin_timeout_120")],
        [InlineKeyboardButton(f"300 секунд (5 мин) ({'✅' if current_timeout == 300 else '❌'})", 
                            callback_data="admin_timeout_300")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_performance")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"""
⏱️ **ВЫБОР ТАЙМАУТА ЗАПРОСОВ**

**Текущий таймаут**: {current_timeout} секунд

**Доступные значения:**
- **30 секунд** - быстро, но может не успеть
- **60 секунд** - рекомендуемое значение
- **120 секунд** - для медленных соединений
- **300 секунд** - для очень медленных соединений

⚠️ **Внимание**: Меньший таймаут = быстрее, больший таймаут = надежнее
"""
    
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_timeout_change(update: Update, context: ContextTypes.DEFAULT_TYPE, timeout: str):
    """Изменяет таймаут запросов"""
    timeout_value = int(timeout)
    success = await update_setting("REQUEST_TIMEOUT_SECONDS", timeout_value)
    
    if success:
        await update.callback_query.answer(f"✅ Таймаут изменен на: {timeout_value} секунд")
        await show_performance_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении таймаута", show_alert=True)

# Обработчики настроек функций
async def handle_prompt_simplification_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает упрощение промптов"""
    current = await get_current_setting("ENABLE_PROMPT_SIMPLIFICATION")
    new_value = not current
    
    success = await update_setting("ENABLE_PROMPT_SIMPLIFICATION", new_value)
    
    if success:
        status = "включено" if new_value else "отключено"
        await update.callback_query.answer(f"✅ Упрощение промптов {status}")
        await show_features_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении настройки", show_alert=True)

async def handle_system_fallback_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает system instruction fallback"""
    current = await get_current_setting("ENABLE_SYSTEM_INSTRUCTION_FALLBACK")
    new_value = not current
    
    success = await update_setting("ENABLE_SYSTEM_INSTRUCTION_FALLBACK", new_value)
    
    if success:
        status = "включен" if new_value else "отключен"
        await update.callback_query.answer(f"✅ System instruction fallback {status}")
        await show_features_menu(update, context)
    else:
        await update.callback_query.answer("❌ Ошибка при изменении настройки", show_alert=True)
