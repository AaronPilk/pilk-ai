"""Agent integrations — manifest schema + /agents response shape.

Covers three things:

1. The manifest accepts a valid ``integrations:`` list and round-trips
   via :class:`Manifest`.
2. Real on-disk manifests (sales_ops, pitch_deck, xauusd) declare the
   integrations they need, so the panel isn't empty on a fresh install.
3. The ``/agents`` HTTP response stamps each agent with an
   ``integrations`` array that tracks ``configured`` state against the
   live stores.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.config import get_settings
from core.db import ensure_schema
from core.registry.manifest import IntegrationSpec, Manifest
from core.secrets import IntegrationSecretsStore

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"


def test_manifest_accepts_api_key_integration() -> None:
    m = Manifest.model_validate(
        {
            "name": "demo_agent",
            "system_prompt": "demo",
            "tools": ["fs_read"],
            "sandbox": {"type": "process", "profile": "demo_agent"},
            "integrations": [
                {
                    "name": "higgsfield_api_key",
                    "kind": "api_key",
                    "label": "Higgsfield API key",
                    "docs_url": "https://higgsfield.ai/",
                },
            ],
        }
    )
    assert len(m.integrations) == 1
    i = m.integrations[0]
    assert isinstance(i, IntegrationSpec)
    assert i.kind == "api_key"
    assert i.docs_url == "https://higgsfield.ai/"


def test_manifest_accepts_oauth_integration() -> None:
    m = Manifest.model_validate(
        {
            "name": "demo_agent",
            "system_prompt": "demo",
            "tools": ["fs_read"],
            "sandbox": {"type": "process", "profile": "demo_agent"},
            "integrations": [
                {
                    "name": "google",
                    "kind": "oauth",
                    "role": "user",
                    "label": "Google",
                    "scopes": ["gmail.send"],
                },
            ],
        }
    )
    i = m.integrations[0]
    assert i.kind == "oauth"
    assert i.role == "user"
    assert i.scopes == ["gmail.send"]


def test_manifest_without_integrations_defaults_to_empty() -> None:
    m = Manifest.model_validate(
        {
            "name": "demo_agent",
            "system_prompt": "demo",
            "tools": ["fs_read"],
            "sandbox": {"type": "process", "profile": "demo_agent"},
        }
    )
    assert m.integrations == []


def test_sales_ops_manifest_declares_keys() -> None:
    path = AGENTS_DIR / "sales_ops_agent" / "manifest.yaml"
    manifest = Manifest.load(path)
    names = {i.name for i in manifest.integrations}
    # The agent's sales-ops loop reads HubSpot + Hunter + Places +
    # PageSpeed + Gmail — any one missing makes the run fail, so the
    # panel must surface them all.
    assert "hubspot_private_token" in names
    assert "hunter_io_api_key" in names
    assert "google_places_api_key" in names
    assert "pagespeed_api_key" in names
    assert "google" in names
    google = next(i for i in manifest.integrations if i.name == "google")
    assert google.kind == "oauth"
    assert google.role == "user"


def test_pitch_deck_manifest_uses_system_role_google() -> None:
    path = AGENTS_DIR / "pitch_deck_agent" / "manifest.yaml"
    manifest = Manifest.load(path)
    oauths = [i for i in manifest.integrations if i.kind == "oauth"]
    assert len(oauths) == 1
    g = oauths[0]
    assert g.name == "google"
    assert g.role == "system"  # deck generation uses system-owned account


def test_xauusd_manifest_declares_browserbase() -> None:
    path = AGENTS_DIR / "xauusd_execution_agent" / "manifest.yaml"
    manifest = Manifest.load(path)
    names = {i.name for i in manifest.integrations}
    assert "browserbase_api_key" in names
    assert "browserbase_project_id" in names


@pytest.fixture
def client() -> TestClient:
    settings = get_settings()
    ensure_schema(settings.db_path)
    return TestClient(create_app())


def test_agents_route_returns_integrations(client: TestClient) -> None:
    with client:
        r = client.get("/agents")
    assert r.status_code == 200
    body = r.json()
    by_name = {a["name"]: a for a in body["agents"]}
    sales = by_name.get("sales_ops_agent")
    assert sales is not None
    assert isinstance(sales.get("integrations"), list)
    assert len(sales["integrations"]) > 0
    # Each entry carries the full UI contract (label + configured flag).
    for entry in sales["integrations"]:
        assert "name" in entry
        assert entry["kind"] in ("api_key", "oauth")
        assert "label" in entry
        assert "configured" in entry


def test_agents_route_reflects_configured_api_key(client: TestClient) -> None:
    """Writing an integration_secret row for a manifest-declared key
    must flip ``configured`` to True in the next /agents response."""
    with client:
        settings = get_settings()
        secrets = IntegrationSecretsStore(settings.db_path)
        secrets.upsert("hubspot_private_token", "pat-test-12345")
        try:
            r = client.get("/agents")
            by_name = {a["name"]: a for a in r.json()["agents"]}
            sales = by_name["sales_ops_agent"]
            hubspot = next(
                i
                for i in sales["integrations"]
                if i["name"] == "hubspot_private_token"
            )
            assert hubspot["configured"] is True
        finally:
            secrets.delete("hubspot_private_token")


def test_integration_spec_round_trips_through_yaml(tmp_path: Path) -> None:
    """Writing a manifest to yaml and re-reading must preserve the
    integration entries exactly — no dropped fields, no coerced types."""
    src = {
        "name": "demo_agent",
        "system_prompt": "demo",
        "tools": ["fs_read"],
        "sandbox": {"type": "process", "profile": "demo_agent"},
        "integrations": [
            {
                "name": "nano_banana_api_key",
                "kind": "api_key",
                "label": "Nano Banana",
                "docs_url": "https://example.invalid/banana",
            },
            {
                "name": "google",
                "kind": "oauth",
                "role": "user",
                "label": "Google",
                "scopes": ["gmail.send", "drive.file"],
            },
        ],
    }
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(src))
    round_tripped = Manifest.load(path)
    assert [i.name for i in round_tripped.integrations] == [
        "nano_banana_api_key",
        "google",
    ]
    gi = round_tripped.integrations[1]
    assert gi.scopes == ["gmail.send", "drive.file"]
