
DROP POLICY IF EXISTS key_usage_policy ON key_usage;
CREATE POLICY key_usage_policy ON key_usage
FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));

DROP POLICY IF EXISTS tavily_key_usage_policy ON tavily_key_usage;
CREATE POLICY tavily_key_usage_policy ON tavily_key_usage
FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));

DROP POLICY IF EXISTS openrouter_key_usage_policy ON openrouter_key_usage;
CREATE POLICY openrouter_key_usage_policy ON openrouter_key_usage
FOR ALL USING ((SELECT current_setting('app.is_admin', true)::boolean = true));
