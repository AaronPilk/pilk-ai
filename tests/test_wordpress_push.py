"""wordpress_push tool tests — offline via httpx.MockTransport."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.policy.risk import RiskClass
from core.tools.builtin.design.wordpress_push import (
    _basic_auth,
    _load_elementor_content,
    _parse_credential,
    wordpress_push_tool,
)
from core.tools.registry import ToolContext

# ── In-memory transport wiring ──────────────────────────────────


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _install_secret(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str | None
) -> None:
    """Stub resolve_secret so tests don't touch the real SQLite store."""
    from core.tools.builtin.design import wordpress_push as mod

    def fake(key: str, env_fallback: str | None = None) -> str | None:
        if key == name:
            return value
        return env_fallback

    monkeypatch.setattr(mod, "resolve_secret", fake)


# ── Credential parsing ─────────────────────────────────────────


def test_parse_credential_happy() -> None:
    assert _parse_credential("user:pass") == ("user", "pass")


def test_parse_credential_strips_spaces_in_password() -> None:
    # WP generates app passwords with spaces for display; callers paste
    # them as-is. We strip quietly.
    assert _parse_credential("user:ab cd ef gh ij kl") == (
        "user",
        "abcdefghijkl",
    )


def test_parse_credential_rejects_no_colon() -> None:
    assert _parse_credential("nopassword") is None


def test_parse_credential_rejects_multiple_colons() -> None:
    # Refuse rather than silently-split on the first one — a password
    # that happens to contain ":" would produce a wrong split and
    # fail WP auth mysteriously. Better to ask the operator to fix it.
    assert _parse_credential("user:pass:extra") is None


def test_parse_credential_rejects_empty_half() -> None:
    assert _parse_credential(":pass") is None
    assert _parse_credential("user:") is None


def test_basic_auth_header_shape() -> None:
    header = _basic_auth("user", "pass")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
    assert decoded == "user:pass"


# ── Elementor JSON loader ──────────────────────────────────────


def test_load_content_from_template_export(tmp_path: Path) -> None:
    path = tmp_path / "template.json"
    path.write_text(
        json.dumps({"version": "0.4", "content": [{"id": "a"}, {"id": "b"}]})
    )
    content = _load_elementor_content(path)
    assert content == [{"id": "a"}, {"id": "b"}]


def test_load_content_from_bare_array(tmp_path: Path) -> None:
    path = tmp_path / "content.json"
    path.write_text(json.dumps([{"id": "a"}]))
    assert _load_elementor_content(path) == [{"id": "a"}]


def test_load_content_wraps_solitary_object(tmp_path: Path) -> None:
    path = tmp_path / "weird.json"
    path.write_text(json.dumps({"some": "object"}))
    assert _load_elementor_content(path) == [{"some": "object"}]


def test_load_content_missing_path_errors(tmp_path: Path) -> None:
    from core.tools.builtin.design.wordpress_push import _LoadError

    with pytest.raises(_LoadError, match="not found"):
        _load_elementor_content(tmp_path / "nope.json")


def test_load_content_invalid_json(tmp_path: Path) -> None:
    from core.tools.builtin.design.wordpress_push import _LoadError

    path = tmp_path / "bad.json"
    path.write_text("{ not json")
    with pytest.raises(_LoadError, match="not valid JSON"):
        _load_elementor_content(path)


# ── Tool risk class ────────────────────────────────────────────


def test_risk_class_is_net_write() -> None:
    """Approval gate keys off this — regressions here would silently
    make WP pushes skip operator confirmation."""
    assert wordpress_push_tool.risk == RiskClass.NET_WRITE


# ── Arg validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_site_url() -> None:
    out = await wordpress_push_tool.handler(
        {
            "target": "new",
            "title": "t",
            "elementor_json_path": "/x",
            "secret_key": "wordpress_x_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "site_url" in out.content


@pytest.mark.asyncio
async def test_invalid_target() -> None:
    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://x.com",
            "target": "bogus",
            "title": "t",
            "elementor_json_path": "/x",
            "secret_key": "wordpress_x_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "target" in out.content


@pytest.mark.asyncio
async def test_target_zero_or_negative_refused() -> None:
    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://x.com",
            "target": 0,
            "title": "t",
            "elementor_json_path": "/x",
            "secret_key": "wordpress_x_app_password",
        },
        ToolContext(),
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_missing_title() -> None:
    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://x.com",
            "target": "new",
            "elementor_json_path": "/x",
            "secret_key": "wordpress_x_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "title" in out.content


# ── Secret resolution ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_secret_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(monkeypatch, "wordpress_acme_app_password", None)
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"content": []}))
    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": "new",
            "title": "Landing",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "not configured" in out.content
    assert "Settings" in out.content


@pytest.mark.asyncio
async def test_malformed_secret_returns_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(
        monkeypatch, "wordpress_acme_app_password", "no-colon-here"
    )
    path = tmp_path / "e.json"
    path.write_text(json.dumps({"content": []}))
    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": "new",
            "title": "Landing",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "username:app_password" in out.content


# ── Happy paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_new_page_happy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(
        monkeypatch, "wordpress_acme_app_password", "editor:ab cd ef"
    )
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content.decode())
        return httpx.Response(
            201,
            json={
                "id": 42,
                "link": "https://acme.com/?p=42",
                "status": "draft",
            },
        )

    _install_transport(monkeypatch, handler)

    elementor_path = tmp_path / "template.json"
    elementor_path.write_text(
        json.dumps({"version": "0.4", "content": [{"id": "abc1234"}]})
    )

    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com/",  # trailing slash deliberately
            "target": "new",
            "title": "Acme Landing",
            "elementor_json_path": str(elementor_path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )

    assert not out.is_error
    assert out.data["page_id"] == 42
    assert out.data["action"] == "created"
    assert captured["url"] == "https://acme.com/wp-json/wp/v2/pages"
    assert captured["method"] == "POST"
    # Basic auth header must use spaces-stripped password.
    hdr = captured["headers"]["authorization"]
    assert hdr.startswith("Basic ")
    decoded = base64.b64decode(hdr.removeprefix("Basic ")).decode()
    assert decoded == "editor:abcdef"
    # Elementor data is a JSON string inside meta._elementor_data.
    meta = captured["body"]["meta"]
    assert meta["_elementor_edit_mode"] == "builder"
    parsed = json.loads(meta["_elementor_data"])
    assert parsed == [{"id": "abc1234"}]
    # New pages always go to draft, never published.
    assert captured["body"]["status"] == "draft"


@pytest.mark.asyncio
async def test_update_existing_page_uses_id_in_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(
        monkeypatch, "wordpress_acme_app_password", "editor:pw"
    )
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(
            200,
            json={"id": 99, "link": "https://acme.com/?p=99", "status": "draft"},
        )

    _install_transport(monkeypatch, handler)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"content": []}))

    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": 99,
            "title": "Acme Landing (update)",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["action"] == "updated"
    assert captured["url"] == "https://acme.com/wp-json/wp/v2/pages/99"


# ── Error responses from WP ────────────────────────────────────


@pytest.mark.asyncio
async def test_wp_401_surfaces_credential_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(monkeypatch, "wordpress_acme_app_password", "user:pw")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "rest_unauthorized"})

    _install_transport(monkeypatch, handler)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"content": []}))

    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": "new",
            "title": "t",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "401" in out.content
    assert "revoked" in out.content or "password" in out.content


@pytest.mark.asyncio
async def test_wp_403_surfaces_capability_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(monkeypatch, "wordpress_acme_app_password", "user:pw")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "rest_forbidden"})

    _install_transport(monkeypatch, handler)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"content": []}))

    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": "new",
            "title": "t",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "edit_pages" in out.content


@pytest.mark.asyncio
async def test_wp_5xx_surfaces_upstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(monkeypatch, "wordpress_acme_app_password", "user:pw")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad gateway")

    _install_transport(monkeypatch, handler)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"content": []}))

    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": "new",
            "title": "t",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "upstream" in out.content


@pytest.mark.asyncio
async def test_transport_error_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_secret(monkeypatch, "wordpress_acme_app_password", "user:pw")

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable")

    _install_transport(monkeypatch, handler)

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"content": []}))

    out = await wordpress_push_tool.handler(
        {
            "site_url": "https://acme.com",
            "target": "new",
            "title": "t",
            "elementor_json_path": str(path),
            "secret_key": "wordpress_acme_app_password",
        },
        ToolContext(),
    )
    assert out.is_error
    assert "transport error" in out.content
