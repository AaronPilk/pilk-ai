from __future__ import annotations

import pytest

from core.tools.builtin.browser import BrowserSessionManager


@pytest.mark.asyncio
async def test_signup_policy_allows_only_one_automated_attempt_per_site() -> None:
    mgr = BrowserSessionManager(api_key="x", project_id="y")

    async def _fake_fill_contact_form(self, session_id, url, fields, submit=True):
        return {
            "url": url,
            "title": "Signup",
            "filled": list(fields.keys()),
            "missed": [],
            "submitted": submit,
        }

    mgr.fill_contact_form = _fake_fill_contact_form.__get__(mgr, BrowserSessionManager)

    first = await mgr.fill_signup_form_once(
        "sess_1",
        "https://trello.com/signup",
        {"email": "x@y.com"},
        plan_id="plan_1",
        submit=True,
    )
    assert first["handoff_required"] is False
    assert first["attempts"] == 1

    second = await mgr.fill_signup_form_once(
        "sess_1",
        "https://trello.com/signup",
        {"email": "x@y.com"},
        plan_id="plan_1",
        submit=True,
    )
    assert second["handoff_required"] is True
    assert "Max automated signup attempts" in second["handoff_reason"]


@pytest.mark.asyncio
async def test_signup_policy_hands_off_immediately_on_captcha() -> None:
    mgr = BrowserSessionManager(api_key="x", project_id="y")

    async def _fake_fill_contact_form(self, session_id, url, fields, submit=True):
        return {
            "url": url,
            "title": "Signup",
            "filled": list(fields.keys()),
            "missed": [],
            "submitted": submit,
        }

    async def _fake_captcha_present(self, page):
        return True

    mgr.fill_contact_form = _fake_fill_contact_form.__get__(mgr, BrowserSessionManager)
    mgr._captcha_present = _fake_captcha_present.__get__(mgr, BrowserSessionManager)
    mgr._pages["sess_2"] = object()

    result = await mgr.fill_signup_form_once(
        "sess_2",
        "https://id.atlassian.com/signup",
        {"email": "x@y.com"},
        plan_id="plan_2",
        submit=True,
    )
    assert result["handoff_required"] is True
    assert "CAPTCHA" in result["handoff_reason"]

