"""Identity layer — connected accounts as first-class objects.

PILK's OAuth-first integrations all land here, regardless of provider.
The rest of the system never looks at Google-specific files or Slack-
specific blobs — it talks to this store through ConnectedAccount records
plus a provider-agnostic token helper.

Directory layout (one place for everything):

    ~/PILK/identity/
      accounts/
        index.json                 public metadata (safe to read widely)
        secrets/
          {account_id}.json        OAuth blob; chmod 600; only AccountsStore reads

One ConnectedAccount per (provider, role, account_id). A per-provider
`defaults` map picks which account a tool binding resolves to when an
exact account_id isn't specified.

This layer does no OAuth itself — it persists what the OAuth layer hands
it, and hands back whatever the tool layer needs. Provider-specific
behavior (auth URLs, scope catalogs, profile fetchers) lives in
`core.integrations.provider`.
"""

from core.identity.accounts import (
    AccountBinding,
    AccountsStore,
    ConnectedAccount,
    account_id_for,
)

__all__ = [
    "AccountBinding",
    "AccountsStore",
    "ConnectedAccount",
    "account_id_for",
]
