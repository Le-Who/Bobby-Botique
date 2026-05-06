import asyncio

from app import database as db


async def main():
    await db.init()
    rows = await db.fetch_all(
        """
        SELECT puzzle_date, target_word, topic, lang, difficulty, hints, image_prompt, image_file_id, image_model, prepared_at
        FROM crocodile_daily_puzzles
        ORDER BY puzzle_date DESC, difficulty ASC
        LIMIT $1
        """,
        5,
    )
    out = []
    for r in rows:
        if r["puzzle_date"] is None:
            continue
        try:
            out.append(
                {
                    "date": r["puzzle_date"].isoformat(),
                    "difficulty": r["difficulty"],
                    "target_word": r["target_word"],
                    "topic": r["topic"],
                    "image_file_id": r["image_file_id"],
                    "image_prompt": r["image_prompt"],
                    "image_model": r["image_model"],
                }
            )
        except Exception as e:
            print(f"Error on row {r}: {e}")
    print("Success! Result:")
    print(out)

asyncio.run(main())
