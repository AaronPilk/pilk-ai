# PILK Portal

Sign-in portal served at **pilk.ai**. A static SPA (Vite + React) that
authenticates users via Supabase Auth and hands them off to their local
PILK daemon.

The portal never touches the daemon directly. Everything after sign-in
is still `http://127.0.0.1:1420` — the local dashboard shipped in
`/ui`. Your data stays on your machine.

## Dev

```bash
cd portal
npm install
cp .env.example .env.local
# fill VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
npm run dev        # http://127.0.0.1:1421
```

## Build

```bash
npm run build      # outputs to portal/dist
```

## Deploy

See [`../docs/portal.md`](../docs/portal.md) for the full Cloudflare
Pages + Supabase Auth setup.
