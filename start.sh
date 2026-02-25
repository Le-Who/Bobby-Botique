#!/bin/bash
set -e

echo "=== BOT STARTUP SCRIPT ==="
echo "Container ID: $HOSTNAME"
echo "Process ID: $$"
echo "Timestamp: $(date)"

# Validate required environment variables
echo "Checking environment variables..."
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: DATABASE_URL not set"
    exit 1
fi
echo "Environment variables OK"

# Start the bot
echo "Starting bot..."
exec python /app/bot.py
