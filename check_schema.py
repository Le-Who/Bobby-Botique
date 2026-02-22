import asyncio
import os
from app.config import settings
import asyncpg

async def check_schema():
    print(f"Connecting to {settings.DATABASE_URL}")
    try:
        conn = await asyncpg.connect(settings.DATABASE_URL)

        # Check tables
        tables = await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        table_names = [t['table_name'] for t in tables]
        print("Tables:", table_names)

        # Check indexes on active_chat_messages
        if 'active_chat_messages' in table_names:
            indexes = await conn.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'active_chat_messages'")
            for idx in indexes:
                print(f"Index: {idx['indexname']} -> {idx['indexdef']}")
        else:
            print("active_chat_messages table MISSING")

        await conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(check_schema())
