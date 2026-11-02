-- Function bodies use public tables and, in two cases, the vector extension's
-- halfvec type. Keep the extension schema resolvable and search pg_temp last.
ALTER FUNCTION public.bump_memory_epoch_on_ltm_disable()
    SET search_path = pg_catalog, public, extensions, pg_temp;
ALTER FUNCTION public.delete_derived_memory_on_source_removal()
    SET search_path = pg_catalog, public, extensions, pg_temp;
ALTER FUNCTION public.recompute_memory_edge_after_source_removal(BIGINT, BIGINT)
    SET search_path = pg_catalog, public, extensions, pg_temp;
ALTER FUNCTION public.delete_orphaned_memory_edge()
    SET search_path = pg_catalog, public, extensions, pg_temp;
ALTER FUNCTION public.recompute_memory_node_after_source_removal()
    SET search_path = pg_catalog, public, extensions, pg_temp;
ALTER FUNCTION public.delete_stale_orphaned_memory_nodes(BIGINT)
    SET search_path = pg_catalog, public, extensions, pg_temp;

-- Older remotely applied policies survived migration 070 under these names.
-- Their settings differ from the runtime context, and permissive policies are
-- ORed together. Retain the canonical policies used by the application.
DROP POLICY IF EXISTS brief_subscriptions_access_policy ON public.brief_subscriptions;
DROP POLICY IF EXISTS branches_access_policy ON public.conversation_branches;
DROP POLICY IF EXISTS reminders_access_policy ON public.user_reminders;

DO $migration$
DECLARE
    target_table TEXT;
    tenant_check TEXT := $policy$
        user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::BIGINT)
        OR (SELECT current_setting('app.is_admin', true)) = 'true'
    $policy$;
BEGIN
    FOREACH target_table IN ARRAY ARRAY[
        'brief_subscriptions', 'conversation_branches', 'user_reminders',
        'horoscope_subscriptions', 'tarot_daily_subscriptions', 'user_achievements'
    ] LOOP
        EXECUTE format(
            'ALTER POLICY %I ON public.%I USING (%s) WITH CHECK (%s)',
            target_table || '_policy', target_table, tenant_check, tenant_check
        );
    END LOOP;
END
$migration$;

ALTER POLICY key_model_status_admin_policy ON public.key_model_status
    USING ((SELECT current_setting('app.is_admin', true)) = 'true')
    WITH CHECK ((SELECT current_setting('app.is_admin', true)) = 'true');

-- These five foreign keys do not have a useful leading-column index.
CREATE INDEX IF NOT EXISTS broadcast_batches_parent_batch_id_idx
    ON public.broadcast_batches (parent_batch_id);
CREATE INDEX IF NOT EXISTS broadcast_recipients_recipient_id_idx
    ON public.broadcast_recipients (recipient_id);
CREATE INDEX IF NOT EXISTS crocodile_daily_prompt_messages_puzzle_date_idx
    ON public.crocodile_daily_prompt_messages (puzzle_date);
CREATE INDEX IF NOT EXISTS crocodile_daily_result_messages_user_id_idx
    ON public.crocodile_daily_result_messages (user_id);
CREATE INDEX IF NOT EXISTS daily_2048_prompt_messages_puzzle_date_idx
    ON public.daily_2048_prompt_messages (puzzle_date);

-- Migration 056 owns this index; the legacy startup seed created a duplicate.
CREATE INDEX IF NOT EXISTS idx_conv_messages_conv_id ON public.conversation_messages (conversation_id);
DROP INDEX IF EXISTS public.idx_conversation_messages_conv_id;
