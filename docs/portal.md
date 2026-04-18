# Portal — sign-in at pilk.ai

The portal is a small static SPA deployed at **pilk.ai**. It does one
job today: **magic-link sign-in** via Supabase Auth. After sign-in it
shows a welcome page and points the user at their local PILK daemon.

PILK itself stays local. The portal doesn't hold your data or your API
keys — future batches will add a BYO-credentials flow that writes keys
into the signed-in user's local daemon, not to the cloud.

## What's in this batch

- `portal/` — Vite + React app (TypeScript).
- `supabase/migrations/0003_users.sql` — `public.users` table, role
  enum, RLS, auto-row trigger on `auth.users` insert, and an
  `is_master_admin()` helper.
- `docs/portal.md` — this file.

Explicitly deferred: portal↔daemon handshake (the portal doesn't talk
to localhost yet), BYO API-keys UI, billing, workspace tables, invites.

## Master admin bootstrap

The sign-up trigger reads `current_setting('app.master_admin_email',
true)` and assigns `role='master_admin'` when the new email matches.
Everyone else gets `role='user'`. Set the GUC once against the Supabase
project:

```bash
supabase secrets set PILK_MASTER_ADMIN_EMAIL="you@example.com"
```

The owner row from migration 0002 stays; the new users row is the
auth-identity counterpart.

## Supabase Auth config (one-time, in the dashboard)

1. **Authentication → Providers → Email**: enable. Default is already
   magic link; leave "Confirm email" on.
2. **Authentication → URL Configuration**:
   - *Site URL*: `https://pilk.ai`
   - *Redirect URLs*: add both
     - `https://pilk.ai/auth/callback`
     - `http://127.0.0.1:1421/auth/callback`  *(for local dev)*

Anything else (Google, GitHub, phone) is deliberately not touched in
this batch.

## Applying the migration

From the repo root:

```bash
supabase link --project-ref <your-project-ref>   # first time only
supabase db push
```

`0003_users.sql` is idempotent — re-running is safe.

## Deploying the portal to Cloudflare Pages

Cloudflare is already serving `pilk.ai`, so Pages is the lowest-friction
host.

1. In the Cloudflare dashboard: **Workers & Pages → Create → Pages →
   Connect to Git**.
2. Pick this repo and the `main` branch.
3. Build settings:
   - Framework preset: **Vite**
   - Build command: `cd portal && npm install && npm run build`
   - Build output directory: `portal/dist`
   - Root directory (advanced): leave blank
4. Environment variables (both "Production" and "Preview"):
   - `VITE_SUPABASE_URL`
   - `VITE_SUPABASE_ANON_KEY`
5. Save and deploy. First build publishes to a `*.pages.dev` URL.
6. Back in the Pages project → **Custom domains → Set up a custom
   domain → pilk.ai**. Cloudflare handles the cert + routing since DNS
   is already in your account.

### SPA fallback (important)

Add a `_redirects` file so deep links (`/auth/callback`, `/signin`) are
served by the SPA instead of returning 404:

```
/*   /index.html   200
```

This lives at `portal/public/_redirects` and is already committed.

## Local dev

```bash
cd portal
npm install
cp .env.example .env.local
# fill VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
npm run dev
# portal: http://127.0.0.1:1421
```

Sign in with a real email; the magic link will redirect to the
configured `http://127.0.0.1:1421/auth/callback` in dev.

## Rolling back

- Delete the Pages project in the Cloudflare dashboard to take the
  portal offline. DNS for `pilk.ai` can keep pointing anywhere you like.
- Revert the migration by writing a `0004_revert_users.sql` that drops
  the table, trigger, helper function, and enum (in that order). No
  other batch depends on `public.users` yet.
