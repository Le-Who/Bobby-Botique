## 2024-10-26 - [Batch Inserts with asyncpg]
**Learning:** `asyncpg` allows efficient batch insertions using `unnest` and array parameters, which is significantly faster than looping over `await db_query`.
**Action:** Always check for loops containing `INSERT` statements and refactor them to use `unnest($1::type[], $2::type[])` for single round-trip insertions.
