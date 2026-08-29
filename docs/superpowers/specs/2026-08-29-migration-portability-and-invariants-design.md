# Migration Portability and Invariants Design

**Date:** 2026-08-29

**Status:** Approved direction; awaiting written-spec review

**Scope:** PostgreSQL migration portability, RLS correctness, schema validation, fail-fast execution, and CI migration invariants

## Incident and root cause

GitHub Actions run `33244950836` failed while applying the full migration chain to the clean `pgvector/pgvector:pg17` service database. Migrations `000` through `013` committed successfully. Migration `014_add_key_model_status.sql` then attempted to create a policy for the Supabase-specific `service_role`, which does not exist in standard PostgreSQL:

```text
014_add_key_model_status.sql FAILED: role "service_role" does not exist
```

The deployment workflow correctly skipped the release because it consumes only a successful `CI` `workflow_run` for `vps_testai`.

The defect remained latent because the PostgreSQL integration job previously ran only on `main`; `vps_testai` did not exercise a clean database. Production Supabase has the role, and production had already recorded migration `014`, so neither unit tests nor subsequent deploys replayed the faulty statement.

## Audit findings

The audit covers all 73 SQL migration files.

- All 73 files parse successfully as PostgreSQL SQL.
- There are no empty migration files, duplicate version prefixes, or forward table-dependency violations in the current manifest.
- The only explicit policy target for a non-standard database role is the unguarded `service_role` reference in migration `014`.
- Migrations `021` and `022` read `app.current_user_id`, while the application sets `app.user_id`. Migration `014` reads `app.user_is_admin`, while the application sets `app.is_admin`.
- The Horoscope and Tarot subscription migrations install permissive `USING (true)` policies rather than tenant-scoped policies.
- Runtime RLS verification omits six RLS-enabled tables: `brief_subscriptions`, `conversation_branches`, `user_reminders`, `horoscope_subscriptions`, `tarot_daily_subscriptions`, and `user_achievements`.
- The schema catalog omits seven application tables: `crocodile_daily_result_messages`, `daily_trivia_used_keys`, `horoscope_subscriptions`, `inline_boards`, `natal_reports`, `tarot_daily_subscriptions`, and `user_achievements`.
- Schema validation currently runs before numbered migrations and only warns, so it cannot prove that the final schema is complete.
- The application startup runner still invokes legacy DDL after a numbered migration fails.
- Both migration runners rely on version tracking but do not reject duplicate version prefixes before execution.

## Goals

1. Make the complete migration chain portable between Supabase PostgreSQL and standard PostgreSQL with pgvector.
2. Preserve service-role behavior on Supabase without manufacturing Supabase roles in CI.
3. Correct RLS context variables and tenant policies for both fresh and already-migrated databases.
4. Validate the final post-migration schema and RLS surface before seeding or starting the bot.
5. Make migration execution fail closed and operationally idempotent.
6. Add durable tests that prevent the same class of portability and manifest defects.

## Non-goals

- Do not disable or bypass the CI/deploy migration gate.
- Do not create a fake `service_role` in CI.
- Do not rewrite the migration system around Alembic or another framework.
- Do not replay every historical data migration against production.
- Do not introduce destructive data cleanup or force RLS for table owners in this hotfix.
- Do not retrofit checksums for already-applied historical files; current environments do not have trustworthy historical checksums to compare.

## Design

### 1. Portable historical migrations

Migration `014` will create `key_model_status_service_role` only when `pg_roles` contains `service_role`. Its `pg_policies` checks will be scoped to `public`. The policy remains available on Supabase and is skipped on standard PostgreSQL.

The fresh-install versions of migrations `014`, `021`, `022`, `047`, and `054` will use the canonical application settings:

- user context: `NULLIF(current_setting('app.user_id', true), '')::BIGINT`
- admin context: `current_setting('app.is_admin', true) = 'true'`

Horoscope and Tarot subscription policies will be tenant/admin scoped instead of open to every role with table privileges.

Changing these historical files is intentional: clean databases must receive the correct definition. Existing databases will not replay them because their versions are already recorded, so a new forward migration is also required.

### 2. Forward repair migration `070`

`070_normalize_tenant_rls_policies.sql` will repair already-migrated databases inside one transaction managed by the runner.

For each affected table it will:

1. enable RLS idempotently;
2. drop the obsolete or permissive policy names with `DROP POLICY IF EXISTS`;
3. create one canonical `FOR ALL` tenant/admin policy with matching `USING` and `WITH CHECK` behavior;
4. retain the conditional service-role policy on `key_model_status` when that role exists.

Affected tables:

- `key_model_status`
- `brief_subscriptions`
- `conversation_branches`
- `user_reminders`
- `horoscope_subscriptions`
- `tarot_daily_subscriptions`
- `user_achievements`

The migration changes policy definitions only. It does not delete or rewrite application rows.

### 3. Runtime RLS verification

`app/db/rls.py` will include every RLS-enabled application table above. Policy names and templates will match migration `070`, allowing startup to verify the final policy surface consistently.

The service/database-owner bypass model remains unchanged. This hotfix corrects policies for roles that are subject to RLS without changing production connection credentials or forcing RLS on owners.

### 4. Authoritative schema validation

The expected-table catalog will include every durable application table created by migrations. Temporary tables are excluded; `schema_migrations` remains expected because the runner creates it.

Startup order will become:

1. run numbered migrations;
2. stop immediately on failure;
3. validate the completed schema strictly;
4. verify RLS policies;
5. seed initial data.

A missing expected table or inability to inspect the schema will abort startup rather than emit a warning and continue.

### 5. Fail-fast and manifest invariants

Both the standalone deploy runner and the application runner will validate the migration manifest before applying SQL:

- filenames must match the versioned migration naming convention;
- version prefixes must be unique;
- migration files must be non-empty and valid UTF-8;
- files execute in deterministic lexical order.

If a numbered migration fails, the application runner will return the failure without executing legacy DDL. Legacy compatibility checks remain available only after the numbered chain succeeds.

Operational idempotency is defined at the runner level: an applied version is not replayed. New and modified schema-policy DDL will additionally be safe to re-execute through `IF EXISTS`, `IF NOT EXISTS`, or deterministic drop/recreate patterns.

### 6. CI contract

The PostgreSQL integration job will:

1. apply the complete migration chain to a clean pgvector PostgreSQL database;
2. invoke the migration runner a second time and require a no-op success;
3. run `--check` and require zero pending migrations;
4. execute integration tests serially.

The deploy workflow remains fail closed and continues to use the exact CI-verified commit SHA.

## Test strategy

Tests are added before implementation and must fail for the current code.

### Static and unit regressions

- Migration `014` guards `service_role` through `pg_roles`.
- No migration references the legacy `app.current_user_id` or `app.user_is_admin` settings.
- Tenant subscription policies do not contain an open `USING (true)` policy.
- Manifest discovery rejects duplicate versions, invalid names, empty files, and invalid UTF-8.
- Every durable `CREATE TABLE IF NOT EXISTS` target appears in the expected schema catalog.
- Every migration-enabled RLS table appears in runtime RLS configuration.
- Application migration failure skips legacy DDL.
- Startup order is migrations → strict schema validation → RLS → seed.
- CI invokes apply twice and then checks for drift.

### Database verification

- The existing GitHub Actions pgvector service must apply all migrations `000` through `070` on a clean database.
- The second runner invocation must report no pending migrations.
- Integration tests must run only after both checks pass.

### Repository quality gates

- Full pytest suite.
- Ruff check and format check.
- Mypy.
- UTF-8 encoding check.
- `git diff --check`.

## Rollout and failure behavior

The changes will be committed and pushed to `vps_testai`. The agent will wait for both CI and the downstream deploy workflow.

- If a later historical migration exposes another clean-database incompatibility, CI remains failed and deploy remains skipped; the next failure will be diagnosed from its exact log before another change.
- If migration `070` fails against production data or privileges, its transaction rolls back and the deploy stops before replacing the running bot container.
- On success, remote branch SHA, CI SHA, deploy SHA, migration status, and bot health will be verified.

## Acceptance criteria

1. Clean standard PostgreSQL with pgvector applies all migrations successfully.
2. A second migration invocation is a successful no-op and `--check` reports no drift.
3. Supabase-specific policies are created only when their roles exist.
4. Existing databases receive corrected RLS policies through migration `070`.
5. Missing schema/RLS objects or numbered migration failures stop startup before seed and bot launch.
6. Full CI succeeds and triggers a successful deployment of the same commit.
