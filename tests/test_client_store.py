"""ClientStore tests — YAML round-trip + failure modes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.clients import Client, ClientStore, set_client_store
from core.clients.store import get_client_store


@pytest.fixture
def clients_dir(tmp_path: Path) -> Path:
    d = tmp_path / "clients"
    d.mkdir()
    return d


# ── Client model ────────────────────────────────────────────────


def test_client_slug_rejects_whitespace() -> None:
    with pytest.raises(ValidationError):
        Client(slug="bad slug", name="x")


def test_client_slug_rejects_uppercase() -> None:
    with pytest.raises(ValidationError):
        Client(slug="AcmeCo", name="x")


def test_client_slug_rejects_leading_hyphen() -> None:
    with pytest.raises(ValidationError):
        Client(slug="-acme", name="x")


def test_client_email_validator_rejects_no_at_sign() -> None:
    with pytest.raises(ValidationError):
        Client(slug="acme", name="Acme", default_email_recipients=["no-at-sign"])


def test_client_happy_path_minimal() -> None:
    c = Client(slug="acme", name="Acme Corp")
    assert c.slug == "acme"
    assert c.canva_brand_kit_id is None
    assert c.default_email_recipients == []


def test_client_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        Client.model_validate({"slug": "x", "name": "y", "oops": 1})


# ── Store loading ───────────────────────────────────────────────


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_load_single_valid_client(clients_dir: Path) -> None:
    _write(
        clients_dir / "acme.yaml",
        "name: Acme Corp\nstyle_notes: direct voice\n",
    )
    store = ClientStore(clients_dir)
    loaded, errors = store.reload()
    assert (loaded, errors) == (1, 0)
    c = store.get("acme")
    assert c is not None and c.name == "Acme Corp"
    assert c.style_notes == "direct voice"


def test_slug_defaults_to_filename_stem(clients_dir: Path) -> None:
    _write(clients_dir / "widgetco.yaml", "name: Widget Co\n")
    store = ClientStore(clients_dir)
    store.reload()
    assert store.get("widgetco") is not None


def test_explicit_slug_overrides_filename(clients_dir: Path) -> None:
    _write(
        clients_dir / "widgetco.yaml",
        "slug: widgets\nname: Widget Co\n",
    )
    store = ClientStore(clients_dir)
    store.reload()
    assert store.get("widgets") is not None
    assert store.get("widgetco") is None


def test_underscore_prefix_is_skipped(clients_dir: Path) -> None:
    _write(clients_dir / "_example.yaml", "name: Example\n")
    _write(clients_dir / "acme.yaml", "name: Acme\n")
    store = ClientStore(clients_dir)
    loaded, _errors = store.reload()
    assert loaded == 1
    assert store.get("acme") is not None
    assert store.get("_example") is None


def test_invalid_yaml_skipped_and_logged(clients_dir: Path) -> None:
    _write(clients_dir / "broken.yaml", "name: [\nunclosed\n")
    _write(clients_dir / "good.yaml", "name: Good\n")
    store = ClientStore(clients_dir)
    loaded, errors = store.reload()
    # The broken file is counted as an error, the good one loads.
    assert loaded == 1
    assert errors == 1
    assert store.get("good") is not None
    assert store.get("broken") is None


def test_schema_violation_skipped(clients_dir: Path) -> None:
    # Missing required ``name`` → Pydantic rejects, file is skipped.
    _write(clients_dir / "incomplete.yaml", "canva_brand_kit_id: x\n")
    store = ClientStore(clients_dir)
    loaded, errors = store.reload()
    assert loaded == 0
    assert errors == 1


def test_slug_collision_keeps_first_and_logs(clients_dir: Path) -> None:
    _write(clients_dir / "a.yaml", "slug: dup\nname: First\n")
    _write(clients_dir / "b.yaml", "slug: dup\nname: Second\n")
    store = ClientStore(clients_dir)
    loaded, errors = store.reload()
    assert loaded == 1  # only the first wins
    assert errors == 1
    assert store.get("dup").name == "First"


def test_list_sorted_by_slug(clients_dir: Path) -> None:
    _write(clients_dir / "zulu.yaml", "name: Z\n")
    _write(clients_dir / "alpha.yaml", "name: A\n")
    _write(clients_dir / "mike.yaml", "name: M\n")
    store = ClientStore(clients_dir)
    store.reload()
    assert [c.slug for c in store.list()] == ["alpha", "mike", "zulu"]


def test_missing_dir_not_an_error(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "nope")
    loaded, errors = store.reload()
    assert (loaded, errors) == (0, 0)
    assert store.list() == []


def test_unknown_slug_returns_none(clients_dir: Path) -> None:
    store = ClientStore(clients_dir)
    store.reload()
    assert store.get("missing") is None


def test_hot_reload_picks_up_new_file(clients_dir: Path) -> None:
    store = ClientStore(clients_dir)
    store.reload()
    assert store.list() == []
    _write(clients_dir / "new.yaml", "name: New Client\n")
    store.reload()
    assert store.get("new") is not None


def test_hot_reload_drops_removed_file(clients_dir: Path) -> None:
    _write(clients_dir / "gone.yaml", "name: Gone\n")
    store = ClientStore(clients_dir)
    store.reload()
    assert store.get("gone") is not None
    (clients_dir / "gone.yaml").unlink()
    store.reload()
    assert store.get("gone") is None


def test_repo_example_file_is_not_loaded_by_default() -> None:
    """The _example.yaml shipped in the repo must never load as a real
    client — that would put a fake 'example' slug in production."""
    repo_clients = Path(__file__).resolve().parents[1] / "clients"
    store = ClientStore(repo_clients)
    store.reload()
    assert store.get("_example") is None
    assert store.get("example") is None


# ── Singleton wiring ────────────────────────────────────────────


def test_singleton_defaults_to_none() -> None:
    set_client_store(None)
    assert get_client_store() is None


def test_singleton_roundtrip(clients_dir: Path) -> None:
    store = ClientStore(clients_dir)
    set_client_store(store)
    try:
        assert get_client_store() is store
    finally:
        set_client_store(None)
