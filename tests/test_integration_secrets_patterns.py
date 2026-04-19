"""Pattern-based secret support in core/api/routes/integration_secrets.py.

Tests the low-level helpers directly; the HTTP route is thin enough
that wiring it through FastAPI's TestClient here would duplicate
coverage without catching new cases.
"""

from __future__ import annotations

from fastapi import HTTPException

from core.api.routes.integration_secrets import (
    KNOWN_SECRET_PATTERNS,
    KNOWN_SECRETS,
    _ensure_known,
    _match_pattern,
)

# ── Pattern matcher ────────────────────────────────────────────


def test_wordpress_pattern_matches_valid_slug() -> None:
    meta = _match_pattern("wordpress_acme_app_password")
    assert meta is not None
    assert "WordPress" in meta["label"]
    assert "acme" in meta["label"]


def test_wordpress_pattern_accepts_hyphenated_slug() -> None:
    meta = _match_pattern("wordpress_acme-corp_app_password")
    assert meta is not None


def test_wordpress_pattern_rejects_uppercase_slug() -> None:
    assert _match_pattern("wordpress_Acme_app_password") is None


def test_wordpress_pattern_rejects_missing_suffix() -> None:
    assert _match_pattern("wordpress_acme_password") is None


def test_wordpress_pattern_rejects_empty_slug() -> None:
    # Fullmatch requires at least one char in the slug group.
    assert _match_pattern("wordpress__app_password") is None


def test_non_wordpress_name_returns_none() -> None:
    assert _match_pattern("hubspot_private_token") is None


# ── _ensure_known ──────────────────────────────────────────────


def test_ensure_known_accepts_static_entry() -> None:
    _ensure_known("hubspot_private_token")  # does not raise


def test_ensure_known_accepts_pattern_match() -> None:
    _ensure_known("wordpress_acme_app_password")  # does not raise


def test_ensure_known_rejects_typo() -> None:
    try:
        _ensure_known("hubsput_private_token")
    except HTTPException as e:
        assert e.status_code == 400
        assert "unknown" in e.detail
    else:
        raise AssertionError("expected HTTPException")


def test_ensure_known_rejects_malformed_wordpress_name() -> None:
    try:
        _ensure_known("wordpress_acme_password")  # missing 'app_'
    except HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("expected HTTPException")


# ── Catalog sanity ─────────────────────────────────────────────


def test_wordpress_pattern_is_registered() -> None:
    # If someone removes this, the wordpress_push tool silently loses
    # its secret storage path — guard against it.
    patterns = [p.pattern.pattern for p in KNOWN_SECRET_PATTERNS]
    assert any("wordpress" in p for p in patterns)


def test_known_static_entries_unchanged_contract() -> None:
    # Don't shrink the published secret list without noticing.
    expected = {
        "hubspot_private_token",
        "hunter_io_api_key",
        "google_places_api_key",
        "pagespeed_api_key",
        "twelvedata_api_key",
    }
    assert expected.issubset(set(KNOWN_SECRETS))
