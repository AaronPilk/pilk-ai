"""slides_create tool + Deck/Slide model tests.

Mocks the Google API client rather than httpx — ``_do_create`` calls
``creds.build("slides", "v1")`` and chains service objects, so we stub
at that layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.identity import AccountsStore
from core.identity.accounts import OAuthTokens
from core.integrations.google.slides import (
    _build_batch_requests,
    _create_slide_requests,
    _slide_object_id,
    make_slides_tools,
)
from core.integrations.google.slides_models import (
    LAYOUT_TO_GOOGLE,
    Deck,
    Slide,
)
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext

# ── Pydantic models ────────────────────────────────────────────


def test_slide_accepts_valid_layouts() -> None:
    Slide(layout="title", title="x")
    Slide(layout="title_and_body", title="x", body="y")
    Slide(layout="blank")


def test_slide_rejects_invalid_layout() -> None:
    with pytest.raises(ValidationError):
        Slide(layout="fancy_animation")  # not in the enum


def test_slide_empty_text_normalizes_to_none() -> None:
    s = Slide(layout="title", title="   ", body="")
    assert s.title is None
    assert s.body is None


def test_slide_extra_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        Slide.model_validate(
            {"layout": "blank", "color": "red"}
        )


def test_deck_requires_at_least_one_slide() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        Deck(title="t", slides=[])


def test_deck_title_non_empty() -> None:
    with pytest.raises(ValidationError):
        Deck(title="", slides=[Slide(layout="title")])


# ── Layout mapping ─────────────────────────────────────────────


def test_layout_mapping_covers_all_enum_members() -> None:
    from typing import get_args

    from core.integrations.google.slides_models import SlideLayout

    enum_vals = set(get_args(SlideLayout))
    assert set(LAYOUT_TO_GOOGLE) == enum_vals


def test_layout_mapping_uses_google_enum_shape() -> None:
    # Google's predefinedLayout enum is UPPER_SNAKE.
    for v in LAYOUT_TO_GOOGLE.values():
        assert v.isupper()
        assert " " not in v


# ── Batch-request builder ──────────────────────────────────────


def test_batch_for_single_title_slide_reuses_auto_created() -> None:
    deck = Deck(title="x", slides=[Slide(layout="title", title="Hello")])
    requests = _build_batch_requests(deck, first_slide_id="s0")
    # No deleteObject + no createSlide — we reuse the auto-created slide.
    assert not any("deleteObject" in r for r in requests)
    assert not any("createSlide" in r for r in requests)


def test_batch_replaces_auto_created_when_layout_differs() -> None:
    deck = Deck(title="x", slides=[Slide(layout="blank")])
    requests = _build_batch_requests(deck, first_slide_id="s0")
    assert {"deleteObject": {"objectId": "s0"}} in requests
    # And a createSlide for slot 0.
    assert any(
        r.get("createSlide", {}).get("objectId") == _slide_object_id(0)
        for r in requests
    )


def test_batch_multiple_slides_assigns_deterministic_ids() -> None:
    deck = Deck(
        title="x",
        slides=[
            Slide(layout="title", title="Cover"),
            Slide(layout="title_and_body", title="A", body="body A"),
            Slide(layout="section_header", title="Break"),
        ],
    )
    requests = _build_batch_requests(deck, first_slide_id="auto")
    # The non-zero indices use pilk_slide_<n> object IDs.
    created_ids = [
        r["createSlide"]["objectId"]
        for r in requests
        if "createSlide" in r
    ]
    assert created_ids == [_slide_object_id(1), _slide_object_id(2)]


def test_create_slide_requests_emits_createshape_for_title_and_body() -> None:
    slide = Slide(layout="title_and_body", title="T", body="B")
    reqs = _create_slide_requests(slide, index=3)
    ops = [next(iter(r)) for r in reqs]
    # Expect: createSlide → createShape (title) → insertText → createShape (body) → insertText.
    assert ops[0] == "createSlide"
    assert "createShape" in ops
    assert ops.count("insertText") == 2


def test_image_url_emits_createimage_request() -> None:
    slide = Slide(layout="blank", image_url="https://x.com/a.png")
    reqs = _create_slide_requests(slide, index=1)
    assert any("createImage" in r for r in reqs)


# ── Tool layer ─────────────────────────────────────────────────


@pytest.fixture
def accounts(tmp_path: Path) -> AccountsStore:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    return store


def test_slides_tool_has_net_write_risk(accounts: AccountsStore) -> None:
    [tool] = make_slides_tools(accounts)
    assert tool.risk == RiskClass.NET_WRITE
    assert tool.name == "slides_create"


@pytest.mark.asyncio
async def test_slides_tool_errors_when_no_account(
    accounts: AccountsStore,
) -> None:
    [tool] = make_slides_tools(accounts)
    out = await tool.handler(
        {"deck": {"title": "x", "slides": [{"layout": "title"}]}},
        ToolContext(),
    )
    assert out.is_error
    assert "Google account" in out.content or "Connected accounts" in out.content


@pytest.mark.asyncio
async def test_slides_tool_validates_deck_shape(
    accounts: AccountsStore, tmp_path: Path
) -> None:
    # Seed an account so the "not linked" branch doesn't fire.
    accounts.upsert(
        provider="google",
        role="user",
        label="test",
        email="x@test.com",
        username=None,
        scopes=["slides.edit"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["slides.edit"],
        ),
        make_default=True,
    )
    [tool] = make_slides_tools(accounts)
    out = await tool.handler({"deck": {"title": "t", "slides": []}}, ToolContext())
    assert out.is_error
    assert "invalid deck" in out.content or "at least one" in out.content


@pytest.mark.asyncio
async def test_slides_tool_calls_api_with_mocked_creds(
    accounts: AccountsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts.upsert(
        provider="google",
        role="user",
        label="test",
        email="x@test.com",
        username=None,
        scopes=["slides.edit"],
        tokens=OAuthTokens(
            access_token="at",
            refresh_token="rt",
            client_id="cid",
            client_secret="cs",
            scopes=["slides.edit"],
        ),
        make_default=True,
    )

    captured: dict = {}

    class _FakeService:
        def presentations(self):
            return self

        def create(self, body=None):
            captured["create_body"] = body
            return _FakeExecute({"presentationId": "deck-abc", "slides": [{"objectId": "s0"}]})

        def batchUpdate(self, presentationId=None, body=None):  # noqa: N802, N803 — mirrors Google's API signature
            captured["batch_presentation_id"] = presentationId
            captured["batch_requests"] = body["requests"]
            return _FakeExecute({})

    class _FakeExecute:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class _FakeCreds:
        email = "x@test.com"

        def build(self, api, version):
            assert api == "slides"
            assert version == "v1"
            return _FakeService()

    # Patch the creds-loader used by the factory so we skip the OAuth
    # plumbing entirely.
    from core.integrations.google import slides as slides_mod

    def fake_credentials_from_blob(blob):
        return _FakeCreds()

    monkeypatch.setattr(
        slides_mod, "credentials_from_blob", fake_credentials_from_blob
    )

    [tool] = make_slides_tools(accounts)
    out = await tool.handler(
        {
            "deck": {
                "title": "My Deck",
                "slides": [
                    {"layout": "title", "title": "Hello"},
                    {"layout": "title_and_body", "title": "A", "body": "b"},
                ],
            }
        },
        ToolContext(),
    )
    assert not out.is_error
    assert out.data["deck_id"] == "deck-abc"
    assert out.data["slide_count"] == 2
    assert "docs.google.com/presentation" in out.data["url"]
    # Sanity on the API shape we sent.
    assert captured["create_body"] == {"title": "My Deck"}
    # Slide index 1 produces a createSlide request; slide 0 is reused.
    create_slide_calls = [
        r for r in captured["batch_requests"] if "createSlide" in r
    ]
    assert len(create_slide_calls) == 1
