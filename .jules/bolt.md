## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Concurrent Dashboard Fetching
**Learning:** Sequential database queries to populate dashboards and menus are a common bottleneck in this app. Also, fetching full objects (like `get_user_documents()`) just to compute their count (using `len()`) adds significant overhead in DB serialization, network transfer, and Python memory allocation.
**Action:** Replace sequential queries in UI handlers with `asyncio.gather()` to fetch them concurrently, and use `COUNT(*)` queries (like `get_user_document_stats()`) instead of fetching entire arrays when only a count is needed.
