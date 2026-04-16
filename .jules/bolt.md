## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-05-18 - Optimizing `get_start_menu_content` sequential queries
**Learning:** `get_start_menu_content` handler executed 3 sequential async database calls (`get_user_today_request_count`, `get_user_documents`, and `get_conversation_count`) causing 3 separate network roundtrips to the database on a heavily accessed menu. Additionally, it was loading an entire array of document records into memory just to count them (`len(docs)`).
**Action:** Consolidated these into a single subqueried CTE `get_user_activity_summary` in `user_stats.py` to fetch all three user metrics in one database roundtrip, utilizing `COUNT(*)` to prevent out-of-memory array allocations.
