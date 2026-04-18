-- PILK Supabase foundation — migration 0003
--
-- Portal sign-in landing. Adds:
--   * public.users — app-level user record mirroring auth.users, with a
--     role enum. Seeded automatically on new sign-ups via a trigger on
--     auth.users. Matches the owners table pattern (master_admin | user
--     | disabled).
--   * a SECURITY DEFINER `is_master_admin()` helper so RLS policies can
--     check role without tripping their own RLS.
--   * RLS: a signed-in user can select/update their own row; master
--     admins can select everyone. Inserts happen through the trigger
--     under the service role and are otherwise blocked.
--
-- Design notes:
--   * On sign-up, if auth.users.email matches current_setting(
--     'app.master_admin_email', true) the new row gets role
--     'master_admin'; otherwise 'user'. This is the first real promotion
--     point for the master-admin concept (Batch 0002 only seeded it in
--     the owners table).
--   * Deliberately narrow: no workspace/tenant tables yet. Those belong
--     in a follow-up batch when we actually have shared data to scope.

create type public.user_role as enum ('master_admin', 'user', 'disabled');

create table if not exists public.users (
    id          uuid primary key references auth.users(id) on delete cascade,
    email       text not null,
    role        public.user_role not null default 'user',
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists idx_users_role on public.users(role);

-- Reuse touch_updated_at from 0001_owner.sql.
drop trigger if exists users_touch_updated_at on public.users;
create trigger users_touch_updated_at
before update on public.users
for each row execute function public.touch_updated_at();

-- Auto-row on auth.users insert. SECURITY DEFINER so the trigger can
-- write into public.users regardless of the authenticating role.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    master_email text;
    assigned_role public.user_role;
begin
    master_email := current_setting('app.master_admin_email', true);
    if master_email is not null
       and length(master_email) > 0
       and new.email = master_email then
        assigned_role := 'master_admin';
    else
        assigned_role := 'user';
    end if;

    insert into public.users (id, email, role)
    values (new.id, new.email, assigned_role)
    on conflict (id) do nothing;

    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Role-aware lookup. SECURITY DEFINER bypasses RLS on the recursive
-- read back into public.users, avoiding infinite loops on admin
-- policies.
create or replace function public.is_master_admin()
returns boolean
language sql
security definer
set search_path = public
stable
as $$
    select exists (
        select 1 from public.users
        where id = auth.uid() and role = 'master_admin'
    );
$$;

alter table public.users enable row level security;

-- Each signed-in user can read their own row.
drop policy if exists users_self_select on public.users;
create policy users_self_select
    on public.users for select
    using (auth.uid() = id);

-- Master admins can read every row.
drop policy if exists users_master_select on public.users;
create policy users_master_select
    on public.users for select
    using (public.is_master_admin());

-- Only the user themselves can update non-role fields. Role changes
-- are restricted to service-role / master admin via a separate future
-- batch; for now updates that try to change role will be blocked by
-- the trigger below.
drop policy if exists users_self_update on public.users;
create policy users_self_update
    on public.users for update
    using (auth.uid() = id)
    with check (auth.uid() = id);

-- Defence-in-depth: refuse a user-initiated update that changes role.
-- Master-admin role management will arrive with its own migration
-- + policy.
create or replace function public.prevent_role_self_change()
returns trigger
language plpgsql
as $$
begin
    if new.role is distinct from old.role
       and auth.uid() is not null
       and not public.is_master_admin() then
        raise exception 'role changes require master admin';
    end if;
    return new;
end;
$$;

drop trigger if exists users_block_self_role_change on public.users;
create trigger users_block_self_role_change
before update on public.users
for each row execute function public.prevent_role_self_change();
