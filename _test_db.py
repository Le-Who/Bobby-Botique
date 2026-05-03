import asyncio
import asyncpg
import json

async def run():
    conn = await asyncpg.connect('postgres://postgres:postgres@localhost:5432/gemaibot')
    rows = await conn.fetch("SELECT puzzle_date, image_file_id FROM crocodile_daily_puzzles WHERE image_file_id != '' LIMIT 5")
    for r in rows:
        print(r['puzzle_date'], repr(r['image_file_id']))
    await conn.close()

asyncio.run(run())
