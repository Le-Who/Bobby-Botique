ALTER TABLE public.inline_boards ENABLE ROW LEVEL SECURITY;
CREATE POLICY inline_boards_policy ON public.inline_boards
FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');

ALTER TABLE public.global_settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY global_settings_policy ON public.global_settings
FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
