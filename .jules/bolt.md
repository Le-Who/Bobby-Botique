## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-05-18 - Concurrent metric fetching for UI counting
**Learning:** Fetching full objects (e.g., calling `get_user_documents` which fetches rows and builds dicts) merely to compute a list length (`len()`) for UI display is highly inefficient. It wastes DB resources and increases memory and serialization overhead, especially when combined sequentially with other counts (like conversations and daily requests) blocking rendering paths.
**Action:** When populating UI menus or dashboards (e.g., `get_start_menu_content`), avoid sequential queries and full object fetches for counting. Utilize `asyncio.gather()` to fetch required metrics concurrently, and use explicit `COUNT(*)` DB queries (such as those wrapped by `get_user_document_stats()`) to dramatically reduce serialization and memory overhead.
