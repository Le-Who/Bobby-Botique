## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2025-02-28 - Bypassing asyncio.gather limitations for DB queries
**Learning:** In this codebase, running multiple database queries concurrently using `asyncio.gather()` inside handlers causes transaction errors (`RuntimeError`) due to shared connection semantics. This forces sequential execution (N+1 queries) which introduces latency.
**Action:** Instead of fetching independent metrics sequentially or trying to use `asyncio.gather()`, combine the lookups into a single SQL query using atomic subqueries in the `SELECT` clause. This reduces network roundtrips to 1 while respecting the single-transaction context constraint.