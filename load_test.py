import asyncio
import base64
import json
import logging
import os
import random
import time
from datetime import datetime

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_test")

# Configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "test_bot_token")
WEBHOOK_URL = os.getenv("WEBHOOK_LOCAL_URL", f"http://localhost:10000/webhook/{BOT_TOKEN}")
MOCK_AI_PORT = 11434
TOTAL_REQUESTS = 1000
TOTAL_USERS = 100

state = {
    "requests_sent": 0,
    "ai_requests_received": 0,
    "start_time": None,
    "end_time": None,
}


async def mock_ai_handler(request):
    """Mocks Google Gemini / OpenRouter API responses with slight delay to simulate processing."""
    state["ai_requests_received"] += 1
    # Simulate network & processing delay
    await asyncio.sleep(random.uniform(0.1, 0.5))

    # Mock OpenRouter / Gemini response format
    # For simplicity, returning a generic OpenAI-compatible streaming/non-streaming response
    return web.json_response(
        {
            "id": f"chatcmpl-{random.randint(1000, 9999)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "This is a mock response from the load test AI server.",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )


async def start_mock_ai_server():
    """Starts a lightweight aiohttp server to intercept AI calls."""
    app = web.Application()
    # Route for OpenRouter/Gemini generic endpoints
    app.router.add_post("/{tail:.*}", mock_ai_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", MOCK_AI_PORT)
    await site.start()
    logger.info(f"Mock AI server running on http://localhost:{MOCK_AI_PORT}")
    return runner


def generate_telegram_update(update_id, user_id):
    """Generates a fake Telegram update dict representing a message."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": f"User{user_id}",
                "username": f"user_{user_id}",
                "language_code": "en",
            },
            "chat": {"id": user_id, "first_name": f"User{user_id}", "username": f"user_{user_id}", "type": "private"},
            "date": int(time.time()),
            "text": "Hello, bot! This is a load test.",
        },
    }


async def fire_request(session, update_id, user_id):
    """Fires a single webhook request to the bot."""
    payload = generate_telegram_update(update_id, user_id)
    try:
        async with session.post(WEBHOOK_URL, json=payload, timeout=10) as resp:
            state["requests_sent"] += 1
            if resp.status != 200:
                logger.warning(f"Request {update_id} returned status {resp.status}")
    except Exception as e:
        logger.error(f"Failed to send request {update_id}: {e}")


async def run_load_test():
    logger.info(f"Starting load test: {TOTAL_REQUESTS} requests from {TOTAL_USERS} users in 1s.")

    # Generate list of users
    users = [100000 + i for i in range(TOTAL_USERS)]

    state["start_time"] = time.time()

    async with aiohttp.ClientSession() as session:
        tasks = []
        for i in range(TOTAL_REQUESTS):
            update_id = 900000 + i
            user_id = random.choice(users)
            tasks.append(fire_request(session, update_id, user_id))

        # Fire them almost concurrently (in a very tight loop)
        await asyncio.gather(*tasks)

    state["end_time"] = time.time()
    elapsed = state["end_time"] - state["start_time"]
    logger.info(f"Fired {state['requests_sent']} webhook requests in {elapsed:.2f} seconds.")


async def main():
    runner = await start_mock_ai_server()

    logger.info("Waiting 2 seconds for server to stabilize...")
    await asyncio.sleep(2)

    # NOTE: To point the bot to use the Mock AI, you need to set OPENROUTER_API_BASE=http://localhost:11434 / GEMINI_BASE_URL to this mock server.
    # Set the environment variables before starting the bot.

    await run_load_test()

    # Wait for the bot to finish processing the queues
    logger.info("Waiting for the bot to process the queued updates...")
    last_ai_requests = 0
    stable_cycles = 0

    while stable_cycles < 5:
        await asyncio.sleep(2)
        current_ai = state["ai_requests_received"]
        logger.info(f"Progress: {current_ai} AI requests intercepted so far...")
        if current_ai == last_ai_requests and current_ai > 0:
            stable_cycles += 1
        else:
            stable_cycles = 0
        last_ai_requests = current_ai

    logger.info("\n========== LOAD TEST REPORT ==========")
    logger.info(f"Total Webhook Requests Fired: {state['requests_sent']}")
    logger.info(f"Total AI Server Requests Handled: {state['ai_requests_received']}")
    if state["requests_sent"] > 0:
        logger.info(f"Completion Ratio: {(state['ai_requests_received'] / state['requests_sent']) * 100:.2f}%")

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
