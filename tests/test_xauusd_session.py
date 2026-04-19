"""Attached-session singleton tests."""

from __future__ import annotations

import pytest

from core.trading.xauusd.session import (
    NotAttachedError,
    clear_attached_session,
    get_attached_session,
    require_attached_session,
    set_attached_session,
)


@pytest.fixture(autouse=True)
def _reset():
    clear_attached_session()
    yield
    clear_attached_session()


def test_default_is_none() -> None:
    assert get_attached_session() is None


def test_require_raises_when_none() -> None:
    with pytest.raises(NotAttachedError):
        require_attached_session()


def test_set_and_get_round_trip() -> None:
    attached = set_attached_session(
        session_id="bb-123",
        account_type="demo",
        account_id="hug-acc-9",
        note="test",
    )
    assert attached.session_id == "bb-123"
    assert attached.account_type == "demo"
    assert attached.account_id == "hug-acc-9"
    assert attached.note == "test"
    assert get_attached_session() is attached
    assert require_attached_session() is attached


def test_clear_returns_prev() -> None:
    set_attached_session(session_id="bb-1", account_type="live")
    prev = clear_attached_session()
    assert prev is not None and prev.session_id == "bb-1"
    assert get_attached_session() is None


def test_second_set_overwrites_first() -> None:
    set_attached_session(session_id="bb-1", account_type="demo")
    set_attached_session(session_id="bb-2", account_type="live")
    cur = get_attached_session()
    assert cur is not None and cur.session_id == "bb-2"
    assert cur.account_type == "live"
