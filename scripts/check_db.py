import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv(".env")


async def main():
    try:
        conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        res = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'chats'"
        )
        print("TEST DB columns in chats:")
        for r in res:
            print(f" - {r['column_name']} ({r['data_type']})")
        await conn.close()
    except Exception as e:
        print(f"Error testing DB: {e}")


if __name__ == "__main__":
    asyncio.run(main())
