CREATE TABLE IF NOT EXISTS public.user_achievements (
      id SERIAL PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
      achievement_id TEXT NOT NULL,
      unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE(user_id, achievement_id)
  );

CREATE INDEX IF NOT EXISTS idx_user_achievements_user_id ON public.user_achievements(user_id);

ALTER TABLE public.user_achievements ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_achievements_policy ON public.user_achievements;
CREATE POLICY user_achievements_policy ON public.user_achievements
FOR ALL USING (
      user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
      OR (select current_setting('app.is_admin', true)) = 'true'
  )
WITH CHECK (
      user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
      OR (select current_setting('app.is_admin', true)) = 'true'
  );
