# GemAI Bot v2

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)
![Gemini](https://img.shields.io/badge/AI-Google%20Gemini-orange.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

GemAI Bot is a powerful, asynchronous Telegram bot that leverages Google's Gemini AI models to provide intelligent responses, document analysis, and internet research capabilities directly within Telegram.

## 🚀 Key Features

*   **Multi-Model Support**: Switch seamlessly between `gemini-2.5-flash`, `gemini-pro`, and other models.
*   **Document Analysis**: Upload PDF, DOCX, or Images. The bot analyzes them using Gemini's multimodal capabilities.
*   **Internet Research**: Integrated with Tavily API for real-time web search and answers grounded in current events.
*   **Long-Term Memory**: (Optional) Redis-based caching and database storage for context-aware conversations.
*   **Group Chat Support**: Specialized modes for group interactions (admin-only, reply-only, etc.).
*   **Robust Architecture**: Built with `python-telegram-bot` (async), `asyncpg` (PostgreSQL), and `redis`.
*   **Cloud Ready**: Includes a lightweight health-check server for deployment on Render, Northflank, etc.

## 🛠 Architecture

The project follows a monolithic asyncio architecture:

*   **Bot Process**: The core `bot.py` handles Telegram updates via long polling.
*   **Task Queue**: Internal `asyncio` queue for processing heavy tasks like document parsing.
*   **Database**: PostgreSQL for user state, chat history, and metrics.
*   **Web Server (Optional)**: A lightweight Flask app runs in the background to provide `GET /health` endpoints, ensuring the bot stays alive on cloud platforms.

## 📋 Prerequisites

*   Python 3.11+
*   PostgreSQL Database
*   Redis (Optional, for caching)
*   **API Keys**:
    *   Telegram Bot Token
    *   Google Gemini API Key
    *   Tavily API Key (for search features)

## ⚙️ Configuration

Create a `.env` file or set environment variables:

```bash
# Core
TELEGRAM_BOT_TOKEN=your_token_here
DATABASE_URL=postgresql://user:pass@host:5432/dbname
ADMIN_ID=123456789

# AI Providers
GEMINI_API_KEYS=key1,key2,key3
TAVILY_API_KEYS=tvly-xxxx

# Options
ENABLE_WEB_SERVER=true  # Set to false to disable the health-check server
PORT=10000              # Port for the health-check server
```

## 🚀 Installation & Run

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/yourusername/gemaibotv2.git
    cd gemaibotv2
    ```

2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

3.  **Run the bot**:
    ```bash
    python bot.py
    ```

## 🐳 Docker Support

The project includes a `Dockerfile` for containerized deployment.

```bash
docker build -t gemaibot .
docker run -d --env-file .env gemaibot
```

## ☁️ Deployment

### Northflank / Render
The project is designed for PaaS deployment.
1.  Connect your repository.
2.  Set Environment Variables.
3.  The `ENABLE_WEB_SERVER=true` flag (default) ensures the `PORT` is bound, passing health checks.

### VPS / Local
If running on a VPS where you don't need a health check port:
1.  Set `ENABLE_WEB_SERVER=false`.
2.  Run as a systemd service or via Docker.

## 📂 Project Structure

```
gemaibotv2/
├── app/
│   ├── handlers/       # Telegram command/message handlers
│   ├── utils/          # Helper functions
│   ├── config.py       # Configuration management
│   ├── database.py     # Async Database layer
│   ├── web.py          # Flask Health Check Server
│   └── ...
├── bot.py              # Entry point
├── requirements.txt    # Dependencies
└── README.md           # Documentation
```

## 📄 License

MIT License.
