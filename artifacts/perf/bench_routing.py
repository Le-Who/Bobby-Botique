import asyncio
import time
import statistics
from unittest.mock import AsyncMock, patch, MagicMock
from dotenv import load_dotenv

load_dotenv()

from app.handlers import messages
from telegram import Update, Message, User, Chat
from app.database import ChatState
from app.handlers.messages import handle_request
from app.providers import get_provider_router

async def bench_routing():
    print("Setting up benchmark...")
    
    # Mock update
    user = User(id=123, first_name="Test", is_bot=False)
    chat = Chat(id=456, type="private")
    message = Message(
        message_id=1,
        date=None,
        chat=chat,
        text="Hello world",
        from_user=user,
    )
    update = Update(update_id=1, message=message)
    context = MagicMock()
    context.user_data = {}

    # We want to measure how fast `messages.handle_request` sets up and spawns the task!
    # Because that is the blocking part of the Telegram event loop.
    times = []
    
    # Mock things that hit the DB or external APIs
    with (
        patch("app.handlers.messages.bind_request_span"),
        patch("app.handlers.messages.set_request_id"),
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock) as mock_state,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.messages.state.get_user_lock", MagicMock()),
        patch("app.handlers.messages.submit_task", MagicMock()),
        patch("app.handlers.messages.metrics_collector", AsyncMock()),
    ):
        cs = MagicMock()
        cs.user_id = 123
        cs.history = []
        cs.token_count = 0
        cs.search_enabled = False
        cs.model = None
        cs.system_prompt = None
        mock_state.return_value = cs
        
        # Warmup
        await handle_request(update, context)
        
        for _ in range(100):
            t0 = time.perf_counter()
            await handle_request(update, context)
            t1 = time.perf_counter()
            times.append(t1 - t0)

    print(f"Messages handle_request routing benchmark (100 iterations):")
    print(f"Mean latency: {statistics.mean(times) * 1000:.2f} ms")
    print(f"P95 latency:  {statistics.quantiles(times, n=100)[94] * 1000:.2f} ms")
    print(f"Min latency:  {min(times) * 1000:.2f} ms")
    print(f"Max latency:  {max(times) * 1000:.2f} ms")
    
    # Also benchmark ProviderRouter instantiation and key resolution
    provider_times = []
    
    for _ in range(100):
        t0 = time.perf_counter()
        provider = get_provider_for_model("gemini-2.5-flash")
        t1 = time.perf_counter()
        provider_times.append(t1 - t0)
        
    print(f"\nProvider Router resolution benchmark (100 iterations):")
    print(f"Mean latency: {statistics.mean(provider_times) * 1000:.2f} ms")
    print(f"P95 latency:  {statistics.quantiles(provider_times, n=100)[94] * 1000:.2f} ms")
    print(f"Min latency:  {min(provider_times) * 1000:.2f} ms")
    print(f"Max latency:  {max(provider_times) * 1000:.2f} ms")

if __name__ == "__main__":
    asyncio.run(bench_routing())
