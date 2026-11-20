# ADR 0006 — Preserve numbered migration compatibility across database layouts

**Status** Accepted

## Serialize migrations and bridge legacy layouts

The command-line runner and startup runner acquire the same transaction-scoped
advisory lock before initializing or reading version history. Transaction
pooling can change server sessions between transactions, and a pending list
captured before acquiring the lock can replay DDL another replica already
committed. An unsuccessful predecessor stops the rest of the chain.

The early bootstrap migrations consolidated tables that had been created at
runtime or through Supabase. An existing database may record migration 000 and
still lack tables subsequently added to its bootstrap SQL. Migrations 018, 024b
and 071 bridge those layouts. Conditional type conversions cover columns some
installations had already changed. The legacy `chats.history` column could be
dropped when `active_chat_messages` became the history store. User-owned child
rows cascade on deletion. Nullable attribution on shared data can be cleared.

Sources [migration chain](../../scripts/migrations),
[startup runner](../../app/db/migrations.py) and
[command-line runner](../../scripts/migrate.py).

## Keep role and query plan boundaries

RLS policies wrap transaction settings in scalar subqueries so PostgreSQL can
initialize the setting once per statement. Message ownership columns introduced
in migration 005 let row checks avoid a repeated join. Explicit role writes
avoid overlapping the role read policy. Permissive policies combine with OR, so
historical policies with different context settings must be dropped. Security
checks use a fixed PostgreSQL function search path that includes the vector
extension schema where needed and searches `pg_temp` last.

The migration chain supports plain PostgreSQL and Supabase. Supabase API roles
are conditional and cannot read bot-owned tables through the Data API. The roles
catalog remains readable to database users through its policy. Direct
migration and runtime connections share one DSN today, so owner access and
`BYPASSRLS` retain their PostgreSQL behavior. Migration 081 enables RLS on
tables before Python startup provisions its normal policies.

Sources [migration 006](../../scripts/migrations/006_fix_rls_performance.sql),
[migration 079](../../scripts/migrations/079_secure_application_tables.sql),
[migration 080](../../scripts/migrations/080_advisor_security_and_indexes.sql)
and [migration 081](../../scripts/migrations/081_enable_rls_before_startup.sql).

## Index query shapes and migrate embedding spaces

Migration 007 supports active-user lookups by date with a composite
`(metric_date, user_id)` index over rows where `request_count > 0`. A rolling
`CURRENT_DATE` predicate would change index membership as the calendar
advances. PostgreSQL requires partial-index predicates to be immutable, so
queries supply their date range while the predicate checks only the stored
request count. [Migration 007](../../scripts/migrations/007_add_user_streaks.sql)
and [PostgreSQL index predicate rules](https://www.postgresql.org/docs/17/sql-createindex.html).

The embedding index moved to `halfvec` in migration 008b because the old
3072-dimensional output exceeded the HNSW `vector` limit. Migration 024 moves
the older vector and half-vector layouts to the 768-dimensional embedding
space. Embeddings from different spaces cannot be reused. The migration
truncates only legacy layouts, using PostgreSQL's stored typmod of 772 to
recognize an existing `halfvec(768)` column and avoid deleting its data.

Predicate deduplication indexes rows with predicate vectors. Legacy NULL rows
enter that index when a vector exists. The smaller HNSW settings reflect the
lower predicate cardinality. Partial HNSW indexes also isolate high-traffic
memory wings. Trigram matching complements vector similarity in hybrid search.
Reverse graph traversal needs its own leading target column because the
source-first unique index covers forward lookups. Conversation and other
foreign-key lookups need indexes on their referencing columns. Migration 080
keeps the conversation-message index owned by migration 056 and removes its
duplicate.

Sources [migration 008b](../../scripts/migrations/008b_upgrade_embedding_3072.sql),
[migration 024](../../scripts/migrations/024_upgrade_gemini_v2_768.sql) and
[migration 056](../../scripts/migrations/056_bolt_performance_indexes.sql).

## Preserve model and provider key boundaries

Model-limit seeds use upserts so reruns preserve models registered later.
Health and cooldowns belong to key/model pairs. Migration 015 allowed IDs
from separate Gemini and OpenRouter key tables by replacing a single-table
foreign key with a validating trigger. Migration 038 relaxed the trigger
to a warning because some providers use keys supplied through process settings
with no matching database row. Repository-level key selection stays scoped
to the provider.

Sources [migration 009](../../scripts/migrations/009_sync_model_limits.sql),
[migration 015](../../scripts/migrations/015_fix_key_model_status_fk.sql) and
[migration 038](../../scripts/migrations/038_relax_key_model_status_trigger.sql).

## Keep delivery history and data projections

Conversation board IDs arrive in Telegram's chosen-inline-result event. Chat
and message coordinates become available later, so the first callback supplies
them. Role diaries are keyed by role so later sessions can reuse that role's
context. Daily game prompt coordinates let completion edit the invitation
in place. For Crocodile, the message type selects the text or caption edit.
The different difficulty tracks retain date-level deliveries during the
dual-track migration. Display names are filled in lazily and are optional
for existing accounts.

Scheduled subscriptions use disabled values for omitted times and store
last-delivery evidence. Discovery offers record a separate timestamp even
when the user has never opted in. Scheduled retries and broadcasts journal
new execution ownership while retaining the existing subscription and reminder
tables. Briefing retries use the admitted slot and the persisted retry time.
They remain eligible after the subscription's initial preferred hour has
passed. A Telegram receipt records observed delivery without establishing
exactly-once delivery. Transport attempts have a separate epoch activated by
the application so earlier logical usage counts retain their meaning.

Daily Trivia's revision model retains the legacy JSON projection for
compatibility. Results with a NULL revision continue to resolve the immutable
legacy day. The one-time reset starts on 2026-08-03, preserving earlier puzzles
and results. The migration removes dependent records explicitly where older
tables lack foreign keys. The initial backfill unwraps historical JSON strings
from writes that double-encoded with the registered asyncpg JSONB codec.
Array expansion is guarded so older scalar values cannot abort the migration.
The user-state and Crocodile data repairs address related legacy JSON shapes.

Daily 2048 adds an edit revision without rewriting any result. Daily Tarot
generation metadata belongs to the shared reading cache rather than a user.
An unset daily game choice inherits the configured default.

Sources [Crocodile prompts migration](../../scripts/migrations/042_add_daily_prompt_messages.sql),
[trivia revision migration](../../scripts/migrations/065_daily_trivia_question_bank.sql),
[trivia cutover](../../scripts/migrations/066_reset_daily_trivia_from_2026_08_03.sql)
and [scheduled delivery ownership](../../scripts/migrations/082_scheduled_delivery_ownership.sql).
See [briefing retry contract](../../tests/integration/contracts/test_scheduled_delivery_postgres.py).
