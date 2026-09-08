# Pollinations and Daily Croc operations

Control behavior below is covered by local tests and browser checks with fixtures.
Paid/live provider generation is not performed by those checks.

## Authentication and models

Pollinations generation uses the server-side key from the existing provider-key store or `POLLINATIONS_API_KEY`. A 401/403 requires checking the key/permissions; a 402 requires checking the Pollen balance and key budget. There is no anonymous Flux fallback. Image failures remain best-effort for daily delivery. Crocodile text generation does not use Pollinations.

The public [image catalog](https://gen.pollinations.ai/image/models) supplies current image model IDs, aliases, and publisher metadata. Saved aliases remain usable. The admin UI retains unavailable saved selections instead of silently selecting another model. No keys are sent to the browser.

## Admin Daily Croc

In `/admin_daily#croc`, the shared Gemini default applies to daily and ordinary Crocodile. Each process can override it independently:

| Process | Setting key |
| --- | --- |
| Word generation, including fast words | `daily_croc_text_model_words` |
| Category classification | `daily_croc_text_model_category` |
| Hints, including background prewarming | `daily_croc_text_model_hints` |
| Semantic answer checking | `daily_croc_text_model_judge` |
| Daily image-prompt descriptions | `daily_croc_text_model_image_prompt` |

An empty override (**Наследовать общую**) uses the historical `daily_croc_text_model` default; if that is also empty, the existing automatic scheme remains. Existing selections need no migration. Calls for explicit selections use `google-genai` and existing Gemini key management, not Pollinations.

**Авто** preserves the previous generation scheme. Existing words, image prompts, and prepared session hints are not regenerated automatically. New AI results are cached separately by selected model. Exact-match checks remain local; other answers are judged by the selected Gemini model, which returns both score and hint in one request. An unavailable judge does not consume a player's attempt. Local word banks and deterministic fallback hints remain available.

**Модель изображения по умолчанию** selects an actual Pollinations catalog model for newly created puzzles, not just a provider. The `daily_croc_image_model` setting stores its canonical ID; legacy `pollinations` maps to `qwen-image`, and FTA remains available. Catalog aliases are resolved when saving. A catalog outage leaves the saved setting unchanged. Each existing puzzle keeps its own model; its card selector still controls actual regeneration. Changing a word does not reset that image-model choice.

Ordinary Crocodile currently has no image-generation step. Per-puzzle image overrides remain specific to that daily puzzle; this shared text-model setting does not introduce a new image feature into ordinary games.

## Prepare an arbitrary day

Choose a date in **Подготовка дня**, including a date absent from the calendar:

- **Проверить готовность** reads persisted Easy/Hard assets only; it never creates puzzles or calls a model.
- **Подготовить день** queues both difficulties and fills missing assets without forcing regeneration, changing existing words, or clearing player progress. The page polls until the job finishes and refreshes the calendar.
- Readiness displays word, hints, image prompt, and image separately. Delivery can be ready without art, but **Всё готово** requires all four parts for both difficulties. Missing art due to quota/provider failure is reported as partial, not success.

The endpoints are authenticated `GET /api/admin/dailycroc/day?date=YYYY-MM-DD` and `POST /api/admin/dailycroc/day/prepare` with `{"date":"YYYY-MM-DD"}`. Preparation runs in a managed background task, with Redis ownership/deduplication when available and local guards otherwise. A job has a ten-minute execution timeout; completed/partial/failed status expires after one day. Failed or interrupted preparation can be retried. Image generation still respects the existing hourly quota.

## Natal smoke and dependency validation

The dependency narrative below is a dated implementation record, not a new
validation result. Use `pyproject.toml`, `uv.lock` and the current CI/deploy
workflows for authoritative dependency state.

Both natal CLI entrypoints default to the configured `ADMIN_ID`. That user must already exist in `users`; `--user-id` can select another existing test user. Smoke checks this before generating a report and never creates a synthetic user. City performance thresholds remain unchanged.

The 2026-09-07 Dependency Frontier Audit was produced from a newer baseline than the initial local checkout. Before pushing, the upstream migration to google-genai 2.19.0 and structlog 26.1.0 was integrated and preserved. Compatible patch updates were layered on top: cryptography 50.0.1, pydantic 2.13.5 / pydantic-core 2.46.5, and pypdf 6.16.2. Further upgrades outside the current manifest policy remain deferred. Deployment and paid live generation require a separate operational run.
