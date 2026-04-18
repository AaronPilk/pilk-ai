"""`/integrations/google/{role}/calendar/glance` — three-state smoke."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.config import get_settings
from core.db import ensure_schema
from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens


@pytest.fixture
def client() -> TestClient:
    settings = get_settings()
    ensure_schema(settings.db_path)
    return TestClient(create_app())


def test_glance_unlinked_returns_false(client: TestClient) -> None:
    with client:
        r = client.get("/integrations/google/user/calendar/glance")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is False
    assert body["role"] == "user"
    assert body["events_count"] == 0


def test_glance_scope_missing_when_calendar_scope_absent(client: TestClient) -> None:
    settings = get_settings()
    home = settings.resolve_home()
    store = AccountsStore(home)
    store.ensure_layout()
    # Seed a Google user-role account WITHOUT calendar scope.
    store.upsert(
        provider="google",
        role="user",
        label="Work",
        email="aaron@work.com",
        username=None,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        ),
        make_default=True,
    )
    with client:
        r = client.get("/integrations/google/user/calendar/glance")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert body["scope_missing"] is True
    assert body["email"] == "aaron@work.com"


def test_glance_rejects_unknown_role(client: TestClient) -> None:
    with client:
        r = client.get("/integrations/google/alien/calendar/glance")
    assert r.status_code == 400


def test_inbox_glance_still_works_alongside_calendar(client: TestClient) -> None:
    # Sanity: the existing inbox route didn't regress after the calendar
    # addition (they share the same helpers).
    with client:
        r = client.get("/integrations/google/user/inbox/glance")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "user"
