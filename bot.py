import os
import logging
import threading
import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# Импортируем наши модули
from app import config, database
from app.handlers import commands, messages, callbacks

# ACTIVE_USER_TASKS теперь живет в app.state

def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    
    if not all([config.TELEGRAM_BOT_TOKEN, config.GEMINI_API_KEYS, config.DATABASE_URL, config.TAVILY_API_KEYS]):
        logging.warning("One or more environment variables are not set! Bot may have limited functionality.")

    # Запускаем веб-сервер для health check в отдельном потоке
    # Flask app теперь находится в commands, так как там есть /start, который его использует
    flask_thread = threading.Thread(target=commands.run_flask, daemon=True)
    flask_thread.start()
    logging.info(f"Health check server started on port {config.PORT}.")

    # Инициализируем пул соединений с БД
    logging.info("Initializing database...")
    database.init_db()
    logging.info("Database initialized.")

    # Создаем приложение Telegram
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрируем все обработчики
    commands.register(application)
    callbacks.register(application)
    messages.register(application)

    logging.info("Starting Telegram bot polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
