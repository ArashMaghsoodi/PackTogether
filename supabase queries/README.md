# Supabase SQL queries

Run order:

1. `001_create_tables.sql`
2. `002_actions_history_retention.sql`
3. `003_allow_multiple_active_trips.sql` (required when upgrading older deployments that had the one-active-trip index)

Notes:

- This setup assumes a fresh migration to Supabase (no local SQLite backfill).
- Multi-trip model allows up to 3 active trips per group in service logic (no DB unique index enforcing one active trip).
- `trips` keeps only core metadata.
- `items` rows are linked to `trips` through `trip_id`.
- `actions_history.expires_at` defaults to `now() + 72 hours`.
- Expired history is removed by `cleanup_expired_actions_history()`.
- For automatic deletion, enable `pg_cron` and schedule the cleanup query.
