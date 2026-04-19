# `clients/`

Per-client configuration for the design agents. One YAML file per
client, filename = slug. Loaded by `ClientStore` at daemon startup (and
on `reload()`).

## File format

```yaml
# clients/acme.yaml
slug: acme                                   # optional; defaults to filename stem
name: Acme Corporation

canva_brand_kit_id: BRAND-KIT-UUID-HERE
canva_brand_kit_name: Acme Primary

wordpress_site_url: https://acme.com
# References an IntegrationSecretsStore key name, not the password itself.
# Set the actual password in Settings → API Keys under the same name.
wordpress_secret_key: wordpress_acme_app_password

default_email_recipients:
  - contact@acme.com
  - founder@acme.com

style_notes: |
  Direct, technical voice. Minimalist layouts. No stock photography —
  prefer illustrations from their brand kit. Avoid jargon; the audience
  is procurement, not engineering.
```

Every field other than `name` is optional. Missing fields simply mean
"no default for this client" — the agent will ask the user instead of
silently substituting.

## Rules

- Filename is the slug unless `slug:` overrides it. Slugs must be
  lowercase, alphanumeric + hyphens only (`a-z`, `0-9`, `-`).
- Files starting with `_` (e.g. `_example.yaml`) are **not loaded**.
  Use that prefix for documentation-only examples.
- Invalid YAML or schema errors log a warning and skip the client —
  the daemon keeps booting. Look for `client_load_failed` in the logs.
- The store does **not** persist anywhere else. Editing a file + calling
  `ClientStore.reload()` (or restarting the daemon) is the only
  update path.

## When the agent consults this

Both `web_design_agent` and `pitch_deck_agent` check `ClientStore.get(slug)`
when a user names a client. If there's no entry, the agent asks before
proceeding — never assumes. That's the point of the "ask before you
invent a brand" hard-rule in both manifests.

## Operational notes

- Files land in the same repo as code so they're reviewed + versioned.
- Secrets stay out of these files: `wordpress_secret_key` holds only
  the *name* of a secret row, not its value.
- Phase 2 (per-user Supabase) re-keys this store on `user_id`; the
  file format stays identical.
