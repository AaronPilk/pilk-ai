"""Provider-credential resolver: load_client / is_configured / setup_hint.

The resolver is what powers the Settings → Connected Accounts page's
"is this provider usable?" check. It does filesystem reads for Google
and env-var reads for everyone else; the tests exercise both branches
so a missing credential path surfaces as a clean `configured=False`
rather than a 503 from the OAuth start endpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.integrations.client_secrets import (
    is_configured,
    load_client,
    setup_hint,
)


@dataclass
class _Settings:
    google_client_secret_path: Path


def _settings(path: Path) -> _Settings:
    return _Settings(google_client_secret_path=path)


# ── is_configured ────────────────────────────────────────────────────


def test_is_configured_false_for_unknown_provider(tmp_path: Path) -> None:
    assert (
        is_configured("not-a-real-provider", settings=_settings(tmp_path / "x.json"))
        is False
    )


def test_is_configured_false_when_google_file_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PILK_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("PILK_GOOGLE_CLIENT_SECRET", raising=False)
    assert is_configured("google", settings=_settings(tmp_path / "missing.json")) is False


def test_is_configured_true_when_google_file_present(tmp_path: Path) -> None:
    path = tmp_path / "pilk-google-client.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "gid",
                    "client_secret": "gsec",
                }
            }
        )
    )
    assert is_configured("google", settings=_settings(path)) is True


def test_is_configured_true_from_google_env_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PILK_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("PILK_GOOGLE_CLIENT_SECRET", "gsec")
    assert (
        is_configured("google", settings=_settings(tmp_path / "missing.json"))
        is True
    )


def test_is_configured_env_providers(tmp_path: Path, monkeypatch) -> None:
    for prov, (id_var, secret_var) in {
        "slack": ("PILK_SLACK_CLIENT_ID", "PILK_SLACK_CLIENT_SECRET"),
        "linkedin": ("PILK_LINKEDIN_CLIENT_ID", "PILK_LINKEDIN_CLIENT_SECRET"),
        "x": ("PILK_X_CLIENT_ID", "PILK_X_CLIENT_SECRET"),
        "meta": ("PILK_META_CLIENT_ID", "PILK_META_CLIENT_SECRET"),
    }.items():
        monkeypatch.delenv(id_var, raising=False)
        monkeypatch.delenv(secret_var, raising=False)
        assert is_configured(prov, settings=_settings(tmp_path)) is False
        monkeypatch.setenv(id_var, "id")
        monkeypatch.setenv(secret_var, "secret")
        assert is_configured(prov, settings=_settings(tmp_path)) is True


# ── setup_hint ───────────────────────────────────────────────────────


def test_setup_hint_google_includes_file_path(tmp_path: Path) -> None:
    path = tmp_path / "pilk-google-client.json"
    hint = setup_hint("google", settings=_settings(path))
    assert hint is not None
    assert str(path) in hint
    assert "PILK_GOOGLE_CLIENT_ID" in hint


def test_setup_hint_env_provider_names_both_vars(tmp_path: Path) -> None:
    for prov, id_var in {
        "slack": "PILK_SLACK_CLIENT_ID",
        "linkedin": "PILK_LINKEDIN_CLIENT_ID",
        "x": "PILK_X_CLIENT_ID",
        "meta": "PILK_META_CLIENT_ID",
    }.items():
        hint = setup_hint(prov, settings=_settings(tmp_path))
        assert hint is not None
        assert id_var in hint


def test_setup_hint_unknown_provider_is_none(tmp_path: Path) -> None:
    assert setup_hint("nope", settings=_settings(tmp_path)) is None


# ── load_client sanity (round-trip on Google file) ───────────────────


def test_load_client_google_reads_installed_block(tmp_path: Path) -> None:
    path = tmp_path / "pilk-google-client.json"
    path.write_text(
        json.dumps({"installed": {"client_id": "gid", "client_secret": "gsec"}})
    )
    assert load_client("google", settings=_settings(path)) == ("gid", "gsec")


def test_load_client_google_reads_web_block(tmp_path: Path) -> None:
    path = tmp_path / "pilk-google-client.json"
    path.write_text(
        json.dumps({"web": {"client_id": "wid", "client_secret": "wsec"}})
    )
    assert load_client("google", settings=_settings(path)) == ("wid", "wsec")
