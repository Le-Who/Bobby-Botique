
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS display_name TEXT;
