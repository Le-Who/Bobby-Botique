1. **Optimize `app/handlers/commands.py:stats_command`**
   - Replace the multiple individual queries (`get_user_today_request_count`, `get_user_documents`, and `get_conversation_count`) with the single consolidated query `get_user_activity_summary`.
   - Update imports in `app/handlers/commands.py` to bring in `get_user_activity_summary` from `app.repos.user_stats`.
   - Instead of fetching all full documents just to calculate `len(docs)`, we'll use the `doc_count` returned directly from `get_user_activity_summary`.

2. **Test Changes**
   - Verify that tests pass.
   - Run linter and formatter.

3. **Pre-commit Checks**
   - Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.

4. **Submit PR**
   - Push branch and create PR with the required `⚡ Bolt: ...` format.
