-- PILK Supabase foundation — migration 0002
--
-- Seed the master admin row, exactly once, from a Postgres setting
-- bound to the SUPABASE_MASTER_ADMIN_EMAIL env var at deploy time.
-- The do-block is intentionally permissive about a missing setting
-- so CI / fresh local projects can run migrations without an email
-- configured yet; the row simply isn't seeded and an operator runs
-- it manually later.
--
-- Idempotency guarantees:
--   * only inserts when public.owners is empty, so rotating the env
--     var after seeding is a no-op — you won't accidentally clone
--     ownership by re-running migrations.
--   * safe to re-run after you've manually renamed / re-roled the
--     master admin; subsequent runs see a non-empty table and skip.
--
-- To configure the email for a Supabase project you control:
--
--     supabase secrets set \
--       PILK_MASTER_ADMIN_EMAIL="you@example.com"
--
-- Then re-deploy; the GUC `app.master_admin_email` is read below.

do $$
declare
    admin_email text;
    existing    integer;
begin
    admin_email := current_setting('app.master_admin_email', true);

    select count(*) into existing from public.owners;
    if existing > 0 then
        raise notice 'owners table already populated; skipping seed';
        return;
    end if;

    if admin_email is null or length(admin_email) = 0 then
        raise notice
            'app.master_admin_email not set; master admin seed skipped. '
            'Set it via Supabase secrets or insert manually.';
        return;
    end if;

    insert into public.owners (email, role)
    values (admin_email, 'master_admin');
end;
$$;
