-- Remove the legacy constraint that allowed only one active trip per chat.
-- This is required when upgrading an existing Supabase instance to the new multi-trip model.

begin;

drop index if exists public.trips_one_active_per_chat;

commit;
