## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-04-23 - Asyncio Gather vs Subqueries in DB Sessions
**Learning:** Do not use `asyncio.gather()` to fetch database queries concurrently in handlers (e.g., `get_start_menu_content`) as it causes transaction errors in shared database sessions. Instead of sequential execution, consolidate multiple reads into a single atomic SQL query using subqueries to reduce network roundtrips safely.
**Action:** Consolidate sequential read queries into single DB queries using subqueries instead of `asyncio.gather` for safe concurrent operations in handlers.

## 2024-04-23 - Consolidating Sequential DB Queries in Handlers
**Learning:** Handlers (e.g., `get_start_menu_content` or `stats_command`) often execute multiple sequential database queries to gather required data (e.g., request count, document count, conversation count), which increases network roundtrips and slows down response time. In PostgreSQL, these can be safely consolidated into a single atomic query using subqueries inside a `SELECT` statement.
**Action:** Always look for opportunities to bundle sequential database reads into a single query using subqueries to reduce network roundtrips and latency, rather than resolving them sequentially or using `asyncio.gather`.
