# Pollinations and Daily Croc operations

Implementation reviewed at `8fc19516` on 2026-09-08. Provider availability and
paid/live generation were not revalidated by the documentation audit.

## Authentication and models

Pollinations generation uses the server-side key from the existing provider-key store or `POLLINATIONS_API_KEY`. A 401/403 requires checking the key/permissions; a 402 requires checking the Pollen balance and key budget. There is no anonymous Flux fallback. Image failures remain best-effort for daily delivery. Crocodile text generation does not use Pollinations.

The public [image catalog](https://gen.pollinations.ai/image/models) supplies current image model IDs, aliases, and publisher metadata. Saved aliases remain usable. The admin UI retains unavailable saved selections instead of silently selecting another model. No keys are sent to the browser.

## Admin Daily Croc

In `/admin_daily#croc`, **Текстовая модель Gemini · Крокодил (оба режима)** selects a shared Gemini model for both daily and ordinary Crocodile: AI word generation, category classification, initial hints, and semantic answer checking. Daily image-prompt descriptions use the same model. The historical setting key `daily_croc_text_model` is preserved, so existing selections need no migration. Calls for an explicit selection use `google-genai` and existing Gemini key management, not Pollinations or the multi-provider router.

**Авто** preserves the previous generation scheme. Existing words, image prompts, and prepared session hints are not regenerated automatically. New AI results are cached separately by selected model. Exact-match checks remain local; other answers are judged by the selected Gemini model, which returns both score and hint in one request. An unavailable judge does not consume a player's attempt. Local word banks and deterministic fallback hints remain available.

The image selector in each puzzle card is used by actual regeneration. New puzzles inherit the global image-provider setting; changing a word preserves that puzzle's image model. A failed or busy forced regeneration reports an error and does not claim an old image is a new result. The existing FTA-to-Pollinations fallback remains, and the actual model is recorded on successful generation.

Ordinary Crocodile currently has no image-generation step. Per-puzzle image overrides remain specific to that daily puzzle; this shared text-model setting does not introduce a new image feature into ordinary games.

## Natal smoke and dependency validation

The dependency narrative below is a dated implementation record, not a new
validation result. Use `pyproject.toml`, `uv.lock` and the current CI/deploy
workflows for authoritative dependency state.

Both natal CLI entrypoints default to the configured `ADMIN_ID`. That user must already exist in `users`; `--user-id` can select another existing test user. Smoke checks this before generating a report and never creates a synthetic user. City performance thresholds remain unchanged.

The 2026-09-07 Dependency Frontier Audit was produced from a newer baseline than the initial local checkout. Before pushing, the upstream migration to google-genai 2.19.0 and structlog 26.1.0 was integrated and preserved. Compatible patch updates were layered on top: cryptography 50.0.1, pydantic 2.13.5 / pydantic-core 2.46.5, and pypdf 6.16.2. Further upgrades outside the current manifest policy remain deferred. Deployment and paid live generation require a separate operational run.
