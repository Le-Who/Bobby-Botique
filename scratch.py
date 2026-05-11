import asyncio
from app import database
from app.config import settings

async def main():
    await database.db_manager.init_pool(settings.DATABASE_URL)
    async with database.db_manager.pool.acquire() as conn:
        rows = await conn.fetch("SELECT puzzle_date, difficulty, length(image_prompt), image_prompt FROM crocodile_daily_puzzles WHERE puzzle_date='2026-05-14'")
        for r in rows:
            print(dict(r))

asyncio.run(main())
