import asyncio
import os

import asyncpg


async def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set")
        return
    
    conn = await asyncpg.connect(db_url)
    try:
        print("Clearing global_settings provider_keys...")
        await conn.execute("DELETE FROM global_settings WHERE key_name LIKE 'provider_key:%'")
        
        print("Clearing corrupted API keys from other tables...")
        tables = [
            "api_keys",  # Gemini API keys
            "openrouter_api_keys",
            "opencode_api_keys",
            "freetheai_api_keys",
            "tavily_api_keys"
        ]
        for table in tables:
            try:
                if table == "tavily_api_keys":
                    await conn.execute(f"DELETE FROM {table} WHERE api_key LIKE 'gAAAAA%'")
                else:
                    # Look at columns in keys.py: ak.api_key
                    await conn.execute(f"DELETE FROM {table} WHERE api_key LIKE 'gAAAAA%'")
                print(f"Cleared {table}")
            except Exception as e:
                print(f"Skipping {table}: {e}")
        print("Done.")
    finally:
        await conn.close()

asyncio.run(main())
