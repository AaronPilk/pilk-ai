# Supabase foundation

This batch adds the *smallest safe foundation* for Supabase inside PILK
— nothing in the runtime path uses Supabase yet. PILK still runs
identically from SQLite when the env vars are unset.

## What ships in this batch

- Optional config in `core/config/settings.py`:
  `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
  `SUPABASE_MASTER_ADMIN_EMAIL`.
- Thin async client wrapper at `core/supabase/client.py`. No SDK
  dependency; uses `httpx` directly. Only callable via the health
  route for now.
- Health endpoint: `GET /supabase/health` →
  `{ configured, has_service_role, url_host, reachable }`.
- Two migrations under `supabase/migrations/`:
  `0001_owner.sql` creates a single `owners` table with RLS on, and
  `0002_seed_master_admin.sql` seeds one master-admin row, once, from
  a Postgres GUC bound to `SUPABASE_MASTER_ADMIN_EMAIL`.
- `supabase/config.toml` so the Supabase CLI can link and push.

## What's intentionally deferred

Auth flows, JWT verification, user/session/workspace tables, storage
buckets, RLS policies beyond the owner table, and anything that moves
PILK state off SQLite. Each of those is a separate batch.

## Master admin model

A single row in `public.owners` with `role='master_admin'`, keyed by
email. This is a policy concept, not an auth identity — it can survive
email changes and auth provider swaps. When Supabase Auth lands in a
later batch we'll add a foreign key to `auth.users.id`.

## Local dev: applying the migrations

Install the CLI once:

```bash
brew install supabase/tap/supabase
```

Link to the project and push:

```bash
supabase link --project-ref <your-project-ref>
# set the master admin email as a Postgres GUC so migration 0002 can
# read it at apply time:
supabase secrets set PILK_MASTER_ADMIN_EMAIL="you@example.com"
supabase db push
```

If the secret is missing, migration 0002 logs a notice and skips
seeding. No error; you can seed the row manually later.

## Check the wiring

```bash
# without creds:
curl http://127.0.0.1:7424/supabase/health
# {"configured": false, "reachable": false}

# with creds set:
curl http://127.0.0.1:7424/supabase/health
# {"configured": true, "has_service_role": true,
#  "url_host": "<ref>.supabase.co", "reachable": true}
```

## Rolling back

- Drop in a `0003_revert_owner.sql` and re-apply; nothing else depends
  on the table yet.
- Clear the env vars to take the foundation offline without touching
  the database.
