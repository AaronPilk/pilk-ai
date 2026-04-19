"""Google provider — scope group resolution."""

from __future__ import annotations

from core.integrations.providers.google import (
    SCOPE_CATALOG,
    google_provider,
)

BASE_URIS = {
    SCOPE_CATALOG["openid"].scope_uri,
    SCOPE_CATALOG["userinfo.email"].scope_uri,
    SCOPE_CATALOG["userinfo.profile"].scope_uri,
}


def test_default_user_is_mail_only() -> None:
    scopes = set(google_provider.scopes_for_role("user", None))
    assert SCOPE_CATALOG["gmail.send"].scope_uri in scopes
    assert SCOPE_CATALOG["gmail.readonly"].scope_uri in scopes
    assert SCOPE_CATALOG["gmail.modify"].scope_uri in scopes
    # Drive + Calendar are not included unless asked.
    assert SCOPE_CATALOG["drive.readonly"].scope_uri not in scopes
    assert SCOPE_CATALOG["calendar.readonly"].scope_uri not in scopes


def test_default_system_is_send_only() -> None:
    scopes = set(google_provider.scopes_for_role("system", None))
    assert SCOPE_CATALOG["gmail.send"].scope_uri in scopes
    # System never gets mail read/modify/drive/calendar.
    assert SCOPE_CATALOG["gmail.readonly"].scope_uri not in scopes
    assert SCOPE_CATALOG["gmail.modify"].scope_uri not in scopes
    assert SCOPE_CATALOG["drive.readonly"].scope_uri not in scopes
    assert SCOPE_CATALOG["calendar.readonly"].scope_uri not in scopes
    assert BASE_URIS.issubset(scopes)


def test_drive_group_adds_drive_readonly() -> None:
    scopes = set(google_provider.scopes_for_role("user", ["mail", "drive"]))
    assert SCOPE_CATALOG["drive.readonly"].scope_uri in scopes
    assert SCOPE_CATALOG["gmail.send"].scope_uri in scopes


def test_calendar_group_adds_read_and_events() -> None:
    scopes = set(google_provider.scopes_for_role("user", ["calendar"]))
    assert SCOPE_CATALOG["calendar.readonly"].scope_uri in scopes
    assert SCOPE_CATALOG["calendar.events"].scope_uri in scopes
    # openid + userinfo still present
    assert BASE_URIS.issubset(scopes)


def test_unknown_group_is_ignored() -> None:
    scopes = set(google_provider.scopes_for_role("user", ["mail", "nonsense"]))
    assert SCOPE_CATALOG["gmail.send"].scope_uri in scopes
    assert SCOPE_CATALOG["drive.readonly"].scope_uri not in scopes


def test_provider_exposes_group_catalog() -> None:
    assert set(google_provider.scope_groups) == {
        "mail",
        "drive",
        "calendar",
        "slides",
    }
    assert google_provider.default_scope_groups == ("mail",)
