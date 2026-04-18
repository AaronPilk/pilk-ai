"""LinkedIn provider + post tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.identity import AccountsStore
from core.integrations.linkedin import make_linkedin_tools
from core.integrations.providers.linkedin import SCOPE_CATALOG, linkedin_provider
from core.policy.comms import NEVER_WHITELISTABLE
from core.policy.gate import Decision, Gate, GateInput
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


def test_linkedin_default_groups_request_identity_and_posts() -> None:
    scopes = set(linkedin_provider.scopes_for_role("user", None))
    assert SCOPE_CATALOG["openid"].scope_uri in scopes
    assert SCOPE_CATALOG["profile"].scope_uri in scopes
    assert SCOPE_CATALOG["email"].scope_uri in scopes
    assert SCOPE_CATALOG["w_member_social"].scope_uri in scopes


def test_linkedin_identity_only_group_still_includes_identity() -> None:
    # Even if the caller passes only `identity`, the resolver keeps
    # identity scopes so profile fetch works.
    scopes = set(linkedin_provider.scopes_for_role("user", ["identity"]))
    assert SCOPE_CATALOG["openid"].scope_uri in scopes
    # w_member_social should not be granted when posts group is excluded.
    assert SCOPE_CATALOG["w_member_social"].scope_uri not in scopes


def test_linkedin_is_standard_oauth_no_pkce_no_refresh() -> None:
    # LinkedIn apps without Marketing Developer Platform don't get refresh
    # tokens. We opt out of the requirement so the flow completes.
    assert linkedin_provider.uses_pkce is False
    assert linkedin_provider.requires_refresh_token is False
    assert linkedin_provider.scope_param_name == "scope"
    assert linkedin_provider.token_exchange_mode == "form"


def test_linkedin_post_tool_metadata(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [post] = make_linkedin_tools(store)
    assert post.name == "linkedin_post_as_me"
    assert post.risk == RiskClass.COMMS
    assert post.account_binding is not None
    assert post.account_binding.provider == "linkedin"
    assert post.account_binding.role == "user"


def test_linkedin_post_is_never_trust_whitelistable() -> None:
    assert "linkedin_post_as_me" in NEVER_WHITELISTABLE


def test_linkedin_post_bypasses_trust_rules() -> None:
    gate = Gate()
    outcome = gate.evaluate(
        GateInput(
            tool_name="linkedin_post_as_me",
            risk=RiskClass.COMMS,
            args={"text": "hello"},
        )
    )
    assert outcome.decision == Decision.APPROVE
    assert outcome.bypass_trust is True


@pytest.mark.asyncio
async def test_linkedin_refuses_when_not_linked(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    [post] = make_linkedin_tools(store)
    result = await post.handler({"text": "hi"}, ToolContext())
    assert result.is_error is True
    assert "Connected accounts" in result.content
