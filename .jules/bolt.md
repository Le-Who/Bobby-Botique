## 2024-05-22 - AsyncPG Pool & RLS Context
**Learning:** Using `set_config` for RLS on a connection pool without explicit connection pinning (`async with pool.acquire() as conn`) causes the context to be lost or applied to the wrong connection. This leads to broken security policies and requires redundant queries (set+clear) on every operation if retries are involved.
**Action:** When using RLS with connection pools, always pass the explicit `conn` object to all helper functions and wrap the entire logical unit (set context -> operation -> clear context) in a single connection context.

## 2024-05-24 - Async Lock Contention
**Learning:** Holding an `asyncio.Lock` while performing I/O operations (like database queries) serializes all concurrent requests, defeating the purpose of `asyncio`. In `get_available_gemini_key`, verifying a cached key inside the lock caused 100 concurrent requests to take 5s instead of 0.05s.
**Action:** Always move I/O or long-running validation logic *outside* of the lock. Use the lock only for quick in-memory dictionary access. If validation fails, re-acquire the lock to safely invalidate the cache (double-checked locking).

## 2024-05-25 - Blocking Word Processing
**Learning:** `python-docx` operations are synchronous and CPU-bound. In `_process_word`, instantiating `Document(path)` and iterating over paragraphs blocked the event loop for ~0.6s even for small files.
**Action:** Always offload `python-docx` or similar synchronous library calls to a thread using `asyncio.to_thread` or `loop.run_in_executor`. Extract the synchronous logic into a pure `_sync` static method.

## 2025-02-19 - PDF Processing O(N^2) Bottleneck
**Learning:** Checking the total length of a list of strings by joining them inside a loop (`len('\n'.join(chunks))`) creates an O(N^2) performance bottleneck. For 500 pages, this operation took ~0.02s vs 0.0004s when using a running counter (50x difference).
**Action:** When accumulating text chunks with a size limit, always maintain a separate `current_length` integer counter instead of re-calculating the full string length on every iteration.

## 2025-02-19 - Message Payload O(N) Memory Allocation
**Learning:** Checking or extracting text from message payload parts using list comprehensions and `.join()` like `str(p.get("text", p))` can cause a massive O(N) memory allocation overhead if the dictionary contains binary representations like `inline_data` or `image_url`. Calling `str()` on large dictionaries blindly forces python to stringify the entire binary data.
**Action:** When iterating over conversation history or message parts, use explicit type checks (`isinstance(p, (bytes, bytearray))`) to skip non-text objects. Avoid stringifying dictionaries by explicitly checking for and skipping payloads with `inline_data` or `image_url` keys.
