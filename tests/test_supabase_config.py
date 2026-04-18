"""Supabase foundation — settings parse, client wires, health route.

Nothing in this batch actually talks to a Supabase project in tests.
We verify:
  - settings accept the new env vars without breaking anything else,
  - the client correctly reports is_configured / public_status,
  - the health route returns a sane payload when nothing is wired.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.config import get_settings
from core.supabase import SupabaseClient


def test_settings_accept_supabase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-xyz")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-xyz")
    monkeypatch.setenv("SUPABASE_MASTER_ADMIN_EMAIL", "me@example.com")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.supabase_url == "https://abc.supabase.co"
    assert settings.supabase_anon_key == "anon-xyz"
    assert settings.supabase_service_role_key == "service-xyz"
    assert settings.supabase_master_admin_email == "me@example.com"
    get_settings.cache_clear()


def test_settings_default_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_MASTER_ADMIN_EMAIL",
    ):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.supabase_url is None
    assert settings.supabase_anon_key is None
    get_settings.cache_clear()


def test_client_is_configured_requires_url_and_anon_key() -> None:
    assert SupabaseClient(url=None, anon_key=None, service_role_key=None).is_configured is False
    assert SupabaseClient(url="https://x.supabase.co", anon_key=None, service_role_key=None).is_configured is False
    assert SupabaseClient(url=None, anon_key="a", service_role_key=None).is_configured is False
    full = SupabaseClient(url="https://x.supabase.co", anon_key="a", service_role_key="s")
    assert full.is_configured is True
    assert full.rest_url == "https://x.supabase.co/rest/v1"
    assert full.auth_headers()["apikey"] == "a"
    assert full.auth_headers(service_role=True)["apikey"] == "s"


def test_public_status_shape() -> None:
    c = SupabaseClient(url="https://abc.supabase.co", anon_key="a", service_role_key=None)
    s = c.public_status()
    assert s["configured"] is True
    assert s["has_service_role"] is False
    assert s["url_host"] == "abc.supabase.co"


def test_health_route_returns_unconfigured_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear any env so the app comes up with Supabase unconfigured.
    for k in (
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_MASTER_ADMIN_EMAIL",
    ):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/supabase/health")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["reachable"] is False
    get_settings.cache_clear()
