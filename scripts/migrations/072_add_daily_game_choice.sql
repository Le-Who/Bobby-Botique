-- Player choice overrides the admin's default daily game. NULL inherits the default.
ALTER TABLE public.crocodile_daily_preferences
    ADD COLUMN daily_game TEXT
    CONSTRAINT crocodile_daily_preferences_daily_game_check
    CHECK (daily_game IN ('crocodile', '2048', 'trivia'));
