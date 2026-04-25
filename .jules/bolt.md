## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2025-04-25 - Consolidate Sequential Database Reads
**Learning:** In handlers like `get_start_menu_content` where multiple database metrics are fetched (`get_user_today_request_count`, `get_user_documents`, `get_conversation_count`), executing them sequentially creates multiple network roundtrips. Even worse, fetching full records (like `get_user_documents`) into Python memory just to compute a `len()` wastes immense network and memory bandwidth.
**Action:** Consolidate multiple metrics into a single atomic CTE or subquery approach (e.g., `get_user_activity_summary`) to perform native aggregation (`COUNT(*)`) inside the database, entirely eliminating the O(N) transport overhead for Python.
