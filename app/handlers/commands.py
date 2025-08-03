import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from .. import config
from .. import database as db
from ..utils.formatting import format_key_for_display
from ..utils import time as time_utils
from ..services import genai

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return
    chat_state = await db.get_user_chat(user_id)
    search_status = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    prompt_status = f"`{chat_state.system_prompt[:100]}...`" if chat_state.system_prompt else "Не задан"
    start_text = (
        "Привет! Я ваш личный ассистент.\n\n"
        f"Ваша основная модель для чата: `{chat_state.model}`\n"
        f"Системная инструкция: {prompt_status}\n"
        f"Режим исследования: **{search_status}**\n\n"
        "**Как работает поиск:**\n"
        "- `? вопрос` - быстрый фактический ответ.\n"
        "- `?? вопрос` - глубокое исследование с анализом.\n"
        "- `??` + `картинка` - поиск по картинке.\n\n"
        "**Команды:**\n"
        "/res - вкл/выкл постоянный режим исследования\n"
        "/newchat - начать новый чат\n"
        "/setprompt `[текст]` - задать инструкцию\n"
        "/model - выбрать основную модель для чата\n\n"
        "**Админ-команды:**\n"
        "/keystatus, /credits, /listmodels, /adduser, /deluser, /listusers"
    )
    await update.message.reply_text(start_text, parse_mode='Markdown')

async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    if not context.args:
        chat_state.system_prompt = None
    else:
        chat_state.system_prompt = " ".join(context.args)
    await db.update_user_chat(user_id, chat_state)
    await update.message.reply_text("✅ Системная инструкция обновлена.")

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)
    await update.message.reply_text("Новый чат создан. История и системная инструкция сброшены.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await db.is_authorized(update.effective_user.id): return
    keyboard = [[InlineKeyboardButton(m, callback_data=f"model_{m}")] for m in config.AVAILABLE_MODELS]
    await update.message.reply_text("Выберите основную модель для разговора:", reply_markup=InlineKeyboardMarkup(keyboard))

async def research_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await db.update_user_chat(user_id, chat_state)
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    await update.message.reply_text(f"🌐 Постоянный режим исследования **{status_text}**.", parse_mode='Markdown')

async def key_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    today_pacific = time_utils.get_pacific_date()
    all_keys = await db.db_query("SELECT * FROM api_keys")
    if not all_keys:
        await update.message.reply_text("Нет ключей Gemini в базе данных.")
        return
    status_lines = ["📊 **Статус ключей Gemini на сегодня:**\n"]
    for key_row in all_keys:
        display_name = format_key_for_display(key_row['api_key'])
        status_lines.append(f"🔑 **Ключ `{display_name}`**")
        usage_data = await db.db_query("SELECT model_name, request_count FROM key_usage WHERE key_hash = ? AND usage_date = ?", (key_row['key_hash'], today_pacific))
        if not usage_data:
            status_lines.append("  - _Сегодня не использовался_")
        else:
            for usage in usage_data:
                model_name = usage['model_name']
                count = usage['request_count']
                limit = config.DAILY_LIMITS.get(model_name, 'N/A')
                status_lines.append(f"  - `{model_name}`: {count} / {limit}")
    status_lines.append(f"\nСброс лимитов произойдет в **{time_utils.get_kyiv_reset_time()}** по Киеву.")
    await update.message.reply_text("\n".join(status_lines), parse_mode='Markdown')

async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    current_month = time_utils.get_current_month_str()
    all_keys = await db.db_query("SELECT * FROM tavily_api_keys")
    if not all_keys:
        await update.message.reply_text("Нет ключей Tavily в базе данных.")
        return
    status_lines = [f"📊 **Расход кредитов Tavily на {current_month}:**\n"]
    for key_row in all_keys:
        display_name = format_key_for_display(key_row['api_key'])
        usage = await db.db_query("SELECT credit_usage FROM tavily_key_usage WHERE key_hash = ? AND usage_month = ?", (key_row['key_hash'], current_month))
        count = usage[0]['credit_usage'] if usage else 0
        limit = config.TAVILY_MONTHLY_CREDIT_LIMIT
        status_lines.append(f"🔑 **Ключ `{display_name}`**: {count} / {limit}")
    status_lines.append(f"\nЛимиты сбрасываются 1-го числа каждого месяца.")
    await update.message.reply_text("\n".join(status_lines), parse_mode='Markdown')

async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    key_data = await db.get_available_gemini_key(config.DEFAULT_MODEL)
    if not key_data:
        await update.message.reply_text("Нет доступных API ключей для выполнения запроса.")
        return
    await update.message.reply_text("Запрашиваю список моделей у Google API...")
    try:
        genai.configure(api_key=key_data['api_key'])
        models_list = [f"- `{m.name}`" for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        await update.message.reply_text("✅ **Доступные модели:**\n" + "\n".join(models_list), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_add = int(context.args[0])
        await db.db_query("INSERT INTO users (user_id, is_authorized) VALUES (?, 1) ON CONFLICT (user_id) DO UPDATE SET is_authorized = 1", (user_to_add,))
        await update.message.reply_text(f"Пользователь {user_to_add} добавлен.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /adduser <user_id>")

async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_del = int(context.args[0])
        if user_to_del == config.ADMIN_ID:
            await update.message.reply_text("Нельзя удалить администратора.")
            return
        await db.db_query("UPDATE users SET is_authorized = 0 WHERE user_id = ?", (user_to_del,))
        await update.message.reply_text(f"Доступ для пользователя {user_to_del} отозван.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /deluser <user_id>")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    rows = await db.db_query("SELECT user_id FROM users WHERE is_authorized = 1")
    user_ids = [str(row['user_id']) for row in rows]
    await update.message.reply_text("Авторизованные пользователи:\n" + "\n".join(user_ids))

def register(application: Application):
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("newchat", new_chat_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("setprompt", set_prompt_command))
    application.add_handler(CommandHandler("res", research_mode_command))
    application.add_handler(CommandHandler("keystatus", key_status_command))
    application.add_handler(CommandHandler("credits", credits_command))
    application.add_handler(CommandHandler("listmodels", list_models_command))
    application.add_handler(CommandHandler("adduser", add_user_command))
    application.add_handler(CommandHandler("deluser", del_user_command))
    application.add_handler(CommandHandler("listusers", list_users_command))
