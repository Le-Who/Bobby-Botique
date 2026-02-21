
import asyncio
import time
import os
import asyncpg
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.environ.get("DATABASE_URL")

async def benchmark():
    if not DATABASE_URL:
        print("DATABASE_URL not set. Skipping benchmark.")
        return

    print("Connecting to DB...")
    # Disable statement cache for PgBouncer compatibility
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)

    try:
        # Create a dummy user and conversation for testing
        async with pool.acquire() as conn:
            # Clean up old test data
            await conn.execute("DELETE FROM users WHERE user_id = 999999")
            await conn.execute("INSERT INTO users (user_id, is_authorized) VALUES (999999, 1) ON CONFLICT (user_id) DO NOTHING")

            # Create a test conversation
            conv_id = await conn.fetchval(
                "INSERT INTO conversations (user_id, title, created_at) VALUES (999999, 'Benchmark', CURRENT_TIMESTAMP) RETURNING id"
            )
            print(f"Created test conversation {conv_id}")

            messages = []
            for i in range(100):
                messages.append({'role': 'user', 'content': f'Message {i}'})
                messages.append({'role': 'assistant', 'content': f'Response {i}'})

            # Prepare data
            roles = [m['role'] for m in messages]
            contents = [m['content'] for m in messages]

            print(f"Benchmarking with {len(messages)} messages...")

            # --- Benchmark N+1 ---
            start_time = time.time()
            for role, content in zip(roles, contents):
                await conn.execute(
                    """INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                       VALUES ($1, $2, $3, CURRENT_TIMESTAMP)""",
                    conv_id, role, content
                )
            n_plus_one_duration = time.time() - start_time
            print(f"N+1 Insert Duration: {n_plus_one_duration:.4f}s")

            # Clean up messages
            await conn.execute("DELETE FROM conversation_messages WHERE conversation_id = $1", conv_id)

            # --- Benchmark Batch (Unnest) ---
            start_time = time.time()
            await conn.execute(
                """INSERT INTO conversation_messages (conversation_id, role, content, created_at)
                   SELECT $1, u.role, u.content, CURRENT_TIMESTAMP
                   FROM unnest($2::text[], $3::text[]) AS u(role, content)""",
                conv_id, roles, contents
            )
            batch_duration = time.time() - start_time
            print(f"Batch Insert Duration: {batch_duration:.4f}s")

            improvement = n_plus_one_duration / batch_duration if batch_duration > 0 else 0
            print(f"Speedup: {improvement:.2f}x")

            # Clean up
            await conn.execute("DELETE FROM conversations WHERE id = $1", conv_id)
            await conn.execute("DELETE FROM users WHERE user_id = 999999")

    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(benchmark())
