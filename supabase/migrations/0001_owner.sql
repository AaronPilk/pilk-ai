-- PILK Supabase foundation — migration 0001
--
-- One table: `owners`. Represents the master admin (you) and,
-- eventually, any additional workspace owners. This is the single
-- source of truth for who ultimately controls a PILK deployment.
--
-- Why a dedicated table instead of piggy-backing on auth.users? The
-- master admin is a policy concept, not an auth identity. It survives
-- email changes, auth provider swaps, and lets seeding run before
-- anyone has signed in via Supabase Auth. Later batches will add a
-- foreign key to auth.users.id once auth is actually wired.
--
-- Safe to re-run: the table creation is idempotent and seeding lives
-- in a separate migration that only inserts when the table is empty.

create table if not exists public.owners (
    id          uuid primary key default gen_random_uuid(),
    email       text not null unique,
    role        text not null check (role in ('master_admin', 'owner')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists idx_owners_role on public.owners(role);

-- RLS on by default. No policies yet — every read/write must go
-- through the service role key. Once Supabase Auth lands in a later
-- batch we'll add: "a signed-in user can read their own owner row".
alter table public.owners enable row level security;

-- updated_at trigger: keeps the column honest without leaning on app
-- code to remember. Small enough to fit in this migration.
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists owners_touch_updated_at on public.owners;
create trigger owners_touch_updated_at
before update on public.owners
for each row execute function public.touch_updated_at();
