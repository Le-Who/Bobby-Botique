CREATE TABLE IF NOT EXISTS public.tarot_daily_readings (
    reading_date  DATE        NOT NULL,
    card_name     TEXT        NOT NULL,
    orientation   TEXT        NOT NULL CHECK (orientation IN ('Прямая', 'Перевернутая')),
    language      TEXT        NOT NULL DEFAULT 'ru',
    body_markdown TEXT        NOT NULL,
    model_name    TEXT        NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (reading_date, card_name, orientation, language)
);

CREATE INDEX IF NOT EXISTS idx_tarot_daily_readings_date_lang
    ON public.tarot_daily_readings (reading_date, language);

INSERT INTO public.model_configuration (model_name, daily_limit, provider)
VALUES ('gemini-3.1-flash-lite', 400, 'Google')
ON CONFLICT (model_name) DO UPDATE
    SET daily_limit = GREATEST(COALESCE(public.model_configuration.daily_limit, 0), EXCLUDED.daily_limit),
        provider = EXCLUDED.provider;
