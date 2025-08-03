import os
import logging
import threading
import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from typing import Dict
from flask import Flask

# Импортируем наши модули
from app import config, database, state
from app.handlers import commands, messages, callbacks

# --- WEB SERVER FOR RENDER HEALTH CHECK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "I am alive!", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=config.PORT)

def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    
    if not all([config.TELEGRAM_BOT_TOKEN, config.GEMINI_API_KEYS, config.DATABASE_URL, config.TAVILY_API_KEYS]):
        logging.warning("One or more environment variables are not set! Bot may have limited functionality.")

    # Запускаем веб-сервер для health check в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logging.info(f"Health check server started on port {config.PORT}.")

    # Инициализируем пул соединений с БД
    logging.info("Initializing database...")
    database.init_db()
    logging.info("Database initialized.")

    # Создаем приложение Telegram
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрируем все обработчики
    # (Создаем функцию для чистоты)
    register_all_handlers(application)

    logging.info("Starting Telegram bot polling...")
    application.run_polling()

def register_all_handlers(application: Application):
    """Регистрирует все обработчики команд, сообщений и колбэков."""
    # Command Handlers
    application.add_handler(CommandHandler("start", commands.start_command))
    application.add_handler(CommandHandler("newchat", commands.new_chat_command))
    application.add_handler(CommandHandler("model", commands.model_command))
    application.add_handler(CommandHandler("setprompt", commands.set_prompt_command))
    application.add_handler(CommandHandler("res", commands.research_mode_command))
    
    # Admin Handlers
    application.add_handler(CommandHandler("keystatus", commands.key_status_command))
    application.add_handler(CommandHandler("credits", commands.credits_command))
    application.add_handler(CommandHandler("listmodels", commands.list_models_command))
    application.add_handler(CommandHandler("adduser", commands.add_user_command))
    application.add_handler(CommandHandler("deluser", commands.del_user_command))
    application.add_handler(CommandHandler("listusers", commands.list_users_command))
    
    # Callback Handlers
    application.add_handler(CallbackQueryHandler(callbacks.model_button_callback, pattern="^model_"))
    application.add_handler(CallbackQueryHandler(callbacks.complex_search_callback, pattern="^complex:"))
    application.add_handler(CallbackQueryHandler(callbacks.fallback_callback, pattern="^fallback:"))
    
    # Message Handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages.handle_request))
    application.add_handler(MessageHandler(filters.PHOTO, messages.handle_request))


if __name__ == "__main__":
    main()
