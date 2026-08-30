-- PackTogether actions_history retention (72h)
-- Run this after 001_create_tables.sql.

begin;

create or replace function public.cleanup_expired_actions_history()
returns bigint
language plpgsql
security definer
as $$
declare
  deleted_count bigint;
begin
  delete from public.actions_history
  where expires_at <= now();

  get diagnostics deleted_count = row_count;
  return deleted_count;
end;
$$;

comment on function public.cleanup_expired_actions_history()
  is 'Deletes actions_history rows older than 72 hours and returns deleted row count.';

commit;

-- Optional scheduler (Supabase pg_cron)
-- Uncomment and run if pg_cron is enabled in your Supabase project:
--
-- select cron.schedule(
--   'packtogether-actions-history-cleanup-hourly',
--   '0 * * * *',
--   $$select public.cleanup_expired_actions_history();$$
-- );

-- Manual cleanup command (if you do not enable pg_cron):
-- select public.cleanup_expired_actions_history();
