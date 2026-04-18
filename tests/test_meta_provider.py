"""Meta provider + Facebook Page / Instagram Business tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.integrations.meta import make_meta_tools
from core.integrations.providers.meta import SCOPE_CATALOG, meta_provider
from core.policy.comms import NEVER_WHITELISTABLE
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def test_meta_default_group_is_pages() -> None:
    scopes = set(meta_provider.scopes_for_role("user", None))
    assert SCOPE_CATALOG["pages_manage_posts"].scope_uri in scopes
    assert SCOPE_CATALOG["pages_show_list"].scope_uri in scopes
    # Instagram scopes aren't in the default group.
    assert SCOPE_CATALOG["instagram_content_publish"].scope_uri not in scopes


def test_meta_instagram_group_adds_ig_scopes_plus_page_scopes() -> None:
    scopes = set(meta_provider.scopes_for_role("user", ["instagram"]))
    # IG publish routes through a Page, so the Page scopes come along.
    assert SCOPE_CATALOG["instagram_basic"].scope_uri in scopes
    assert SCOPE_CATALOG["instagram_content_publish"].scope_uri in scopes
    assert SCOPE_CATALOG["pages_show_list"].scope_uri in scopes


def test_meta_both_groups_union() -> None:
    scopes = set(meta_provider.scopes_for_role("user", ["pages", "instagram"]))
    assert SCOPE_CATALOG["pages_manage_posts"].scope_uri in scopes
    assert SCOPE_CATALOG["instagram_content_publish"].scope_uri in scopes


def test_meta_system_role_unsupported_returns_empty() -> None:
    assert meta_provider.supports_roles == ("user",)
    assert meta_provider.scopes_for_role("system", None) == []


def test_meta_tools_metadata(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    tools = make_meta_tools(store)
    names = [t.name for t in tools]
    assert names == ["facebook_post_to_page", "instagram_post_to_business"]
    for t in tools:
        assert t.risk == RiskClass.COMMS
        assert t.account_binding is not None
        assert t.account_binding.provider == "meta"
        assert t.account_binding.role == "user"


def test_meta_post_tools_are_never_trust_whitelistable() -> None:
    assert "facebook_post_to_page" in NEVER_WHITELISTABLE
    assert "instagram_post_to_business" in NEVER_WHITELISTABLE


@pytest.mark.asyncio
async def test_meta_refuses_when_not_linked(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [fb, ig] = make_meta_tools(store)
    fb_result = await fb.handler({"text": "hi"}, ToolContext())
    assert fb_result.is_error is True
    assert "Connected accounts" in fb_result.content
    ig_result = await ig.handler(
        {"caption": "c", "image_url": "https://x/y.jpg"},
        ToolContext(),
    )
    assert ig_result.is_error is True


@pytest.mark.asyncio
async def test_meta_refuses_fb_when_account_has_no_pages(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    store.upsert(
        provider="meta",
        role="user",
        label="Meta",
        email="aaron@example.com",
        username="aaron",
        scopes=[SCOPE_CATALOG["pages_manage_posts"].scope_uri],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="",
            client_id="cid",
            client_secret="cs",
            scopes=[SCOPE_CATALOG["pages_manage_posts"].scope_uri],
            extra={"pages": []},
        ),
        make_default=True,
    )
    [fb, _ig] = make_meta_tools(store)
    result = await fb.handler({"text": "hi"}, ToolContext())
    assert result.is_error is True
    assert "Personal-profile posting" in result.content or "don't manage" in result.content


@pytest.mark.asyncio
async def test_meta_rejects_non_https_image_url(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    store.upsert(
        provider="meta",
        role="user",
        label="Meta",
        email=None,
        username=None,
        scopes=[],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="",
            client_id="cid",
            client_secret="cs",
            scopes=[],
            extra={
                "pages": [
                    {
                        "id": "p1",
                        "name": "My Page",
                        "page_access_token": "patok",
                        "ig_business_id": "ig1",
                    }
                ]
            },
        ),
        make_default=True,
    )
    [_fb, ig] = make_meta_tools(store)
    result = await ig.handler(
        {"caption": "hi", "image_url": "http://insecure/x.jpg"},
        ToolContext(),
    )
    assert result.is_error is True
    assert "https" in result.content
