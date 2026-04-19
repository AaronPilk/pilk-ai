"""Client configuration model.

A :class:`Client` is the per-client hand-off document the design agents
consult when a user names a client in a task. It's a narrow object by
design — five fields, all optional bar slug+name — because the more we
put here, the more agents have to reason about when a client *isn't*
configured.

Source of truth lives in ``clients/<slug>.yaml`` on disk. The store
reads-through at startup and on ``reload()``; there's no SQLite mirror.
Phase 2 (per-user Supabase) will re-key on user_id; the file format
stays stable.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Conservative slug format: lowercase, hyphens, digits. Rejects
# whitespace + punctuation that would break path expansion.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class Client(BaseModel):
    """One client's brand + destination config."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)

    # Canva brand-kit ID (UUID-shaped) the agent should apply when
    # generating designs or decks for this client. Both ID + human
    # name are optional so the fixture doesn't fail validation on a
    # client who isn't using Canva yet.
    canva_brand_kit_id: str | None = None
    canva_brand_kit_name: str | None = None

    # WordPress destination for the Elementor push flow. The URL is
    # public; the secret is a *reference* to an IntegrationSecretsStore
    # row name (e.g. ``"wordpress_acme_app_password"``) — never the
    # password itself. A missing secret simply means "no push target
    # configured yet."
    wordpress_site_url: str | None = Field(default=None, max_length=500)
    wordpress_secret_key: str | None = Field(default=None, max_length=120)

    # Default recipients for `agent_email_deliver` calls when the
    # agent wasn't told explicitly. Kept list-shaped even at length 1
    # so callers don't branch on "is this a string or a list."
    default_email_recipients: list[str] = Field(default_factory=list)

    # Freeform style guidance. The agent injects this into its system
    # prompt for client-scoped runs, so keep it concise — a paragraph,
    # not a brand bible.
    style_notes: str | None = Field(default=None, max_length=2000)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str) -> str:
        if not _SLUG_RE.fullmatch(v):
            raise ValueError(
                "slug must be lowercase letters/digits/hyphens, "
                "starting and ending with alphanumeric"
            )
        return v

    @field_validator("default_email_recipients")
    @classmethod
    def _emails_look_like_emails(cls, v: list[str]) -> list[str]:
        for e in v:
            if "@" not in e or e.strip() != e:
                raise ValueError(f"not a valid email: {e!r}")
        return v


__all__ = ["Client"]
