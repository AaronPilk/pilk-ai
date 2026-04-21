"""Go High Level integration package — foundation.

Replaces HubSpot as PILK's CRM backend. GHL's API surface is deep —
contacts, opportunities/pipelines, conversations (SMS + email),
calendars/appointments, tasks, workflows, tags, custom fields, users,
locations — rolled out across three PRs to keep each change
reviewable:

  #75a  foundation: client + secrets + error shape (this PR)
  #75b  contacts CRUD + meta + HubSpot removal
  #75c  pipelines + conversations
  #75d  calendars + tasks + workflows

### Auth model

Agency-level Private Integration Token (PIT). Bearer header, no
OAuth, no token refresh. Issued once at Settings → Company →
Private Integrations in GHL's agency view with every scope box
checked. Stored as ``ghl_api_key`` in the integration-secrets
store.

### Location scope

GHL calls are per-sub-account. Every tool (future PRs) accepts an
optional ``location_id`` argument; omit it and the tool falls back
to ``ghl_default_location_id`` from settings. The PIT itself
carries agency-wide authority so swapping ``location_id`` between
calls requires no re-auth.
"""

from __future__ import annotations

from core.integrations.ghl.client import (
    GHL_API_BASE,
    GHL_API_VERSION,
    GHLClient,
    GHLError,
    GHLNotConfiguredError,
    client_from_settings,
    resolve_location_id,
)
from core.integrations.ghl.tools import make_ghl_pipeline_tools

__all__ = [
    "GHL_API_BASE",
    "GHL_API_VERSION",
    "GHLClient",
    "GHLError",
    "GHLNotConfiguredError",
    "client_from_settings",
    "make_ghl_pipeline_tools",
    "resolve_location_id",
]
