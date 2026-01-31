## 2024-05-22 - AsyncPG Pool & RLS Context
**Learning:** Using `set_config` for RLS on a connection pool without explicit connection pinning (`async with pool.acquire() as conn`) causes the context to be lost or applied to the wrong connection. This leads to broken security policies and requires redundant queries (set+clear) on every operation if retries are involved.
**Action:** When using RLS with connection pools, always pass the explicit `conn` object to all helper functions and wrap the entire logical unit (set context -> operation -> clear context) in a single connection context.
