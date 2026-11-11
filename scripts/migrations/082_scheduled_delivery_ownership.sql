-- Preserve existing reminders/subscriptions; journal new scheduled executions.
ALTER TABLE public.brief_subscriptions
    ADD COLUMN IF NOT EXISTS delivery_revision bigint NOT NULL DEFAULT 1;

CREATE OR REPLACE FUNCTION public.fence_brief_subscription_delivery()
RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF (NEW.is_active, NEW.timezone, NEW.preferred_hour)
        IS DISTINCT FROM (OLD.is_active, OLD.timezone, OLD.preferred_hour)
        OR NEW.delivery_revision IS DISTINCT FROM OLD.delivery_revision THEN
        NEW.delivery_revision := OLD.delivery_revision + 1;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS brief_subscription_delivery_revision ON public.brief_subscriptions;
CREATE TRIGGER brief_subscription_delivery_revision
    BEFORE UPDATE ON public.brief_subscriptions FOR EACH ROW
    EXECUTE FUNCTION public.fence_brief_subscription_delivery();

CREATE TABLE IF NOT EXISTS public.scheduled_deliveries (
    delivery_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('reminder', 'brief')),
    source_id bigint NOT NULL,
    source_revision bigint NOT NULL,
    slot_date date NOT NULL,
    user_id bigint NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    privacy_epoch bigint NOT NULL,
    owner_token uuid NOT NULL,
    state text NOT NULL DEFAULT 'started'
        CHECK (state IN ('started', 'sent', 'failed', 'skipped', 'unknown_delivery')),
    reason_code text,
    message_id bigint,
    retryable boolean NOT NULL DEFAULT FALSE,
    retry_count smallint NOT NULL DEFAULT 0,
    retry_at timestamptz,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT clock_timestamp() + interval '120 seconds',
    finished_at timestamptz,
    UNIQUE (kind, source_id, slot_date)
);
CREATE INDEX IF NOT EXISTS scheduled_deliveries_user_id_idx ON public.scheduled_deliveries(user_id);
CREATE INDEX IF NOT EXISTS scheduled_deliveries_expiry_idx ON public.scheduled_deliveries(expires_at)
    WHERE state = 'started';

CREATE TABLE IF NOT EXISTS public.scheduled_delivery_attempts (
    attempt_id uuid PRIMARY KEY,
    delivery_id uuid NOT NULL REFERENCES public.scheduled_deliveries(delivery_id) ON DELETE CASCADE,
    owner_token uuid NOT NULL,
    sequence integer NOT NULL,
    outcome text CHECK (outcome IN ('sent', 'failed', 'skipped', 'unknown_delivery')),
    reason_code text,
    message_id bigint,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (delivery_id, sequence)
);

ALTER TABLE public.scheduled_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduled_delivery_attempts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scheduled_deliveries_admin_policy ON public.scheduled_deliveries;
CREATE POLICY scheduled_deliveries_admin_policy ON public.scheduled_deliveries FOR ALL
    USING ((select current_setting('app.is_admin', true)) = 'true')
    WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');
DROP POLICY IF EXISTS scheduled_delivery_attempts_admin_policy ON public.scheduled_delivery_attempts;
CREATE POLICY scheduled_delivery_attempts_admin_policy ON public.scheduled_delivery_attempts FOR ALL
    USING ((select current_setting('app.is_admin', true)) = 'true')
    WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');

DO $migration$
DECLARE target_table text;
DECLARE target_role text;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['scheduled_deliveries', 'scheduled_delivery_attempts'] LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM PUBLIC', target_table);
        FOREACH target_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=target_role) THEN
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE public.%I FROM %I', target_table, target_role);
            END IF;
        END LOOP;
    END LOOP;
END
$migration$;
